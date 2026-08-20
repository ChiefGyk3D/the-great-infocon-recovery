#!/usr/bin/env python3
"""
fetch_defcon_torrents.py

Fetches ALL available DEF CON year/edition archives via BitTorrent v2 using
libtorrent, from the per-item torrents published at
media.defcon.org/DEF CON Torrents/. Those torrents are BitTorrent-v2-only
(meta version 2, "file tree" layout), which neither the system aria2c (1.36)
nor transmission-cli (3.00) support - hence python-libtorrent instead.

Each torrent's internal folder name (its own info.name, e.g. "DEF CON 30" or
"DEF CON Conference CD DVD") is used by libtorrent under --dest, so pointing
--dest at ".../cons/DEF CON" lands files at ".../cons/DEF CON/DEF CON 30/...",
matching the existing drive layout exactly. When a year/edition already
exists locally, libtorrent hash-checks it against the torrent's piece hashes
and only downloads pieces that are missing or don't match - this verifies
existing content and fills in gaps without re-fetching what's already good.

Usage:
    # Fetch/verify everything available via torrent
    python fetch_defcon_torrents.py --dest "/media/chiefgyk3d/infocon.org DC30/cons/DEF CON"

    # Restrict to specific years/editions (substring match)
    python fetch_defcon_torrents.py --dest "..." --only "30,31,32,33"
"""
from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from collections.abc import Callable
from urllib.parse import unquote, urljoin

import libtorrent as lt
from bs4 import BeautifulSoup

TORRENTS_DIR_URL = "https://media.defcon.org/DEF%20CON%20Torrents/"
INFOCON_ROOT_URL = "https://infocon.org/"
USER_AGENT = "InfoConDriveSync/1.0 (personal archive sync tool)"
DEFAULT_TORRENTS_CACHE = os.environ.get(
    "INFOCON_TORRENTS_CACHE",
    os.path.join(os.path.expanduser("~"), ".cache", "infocon-scraper", "torrents"),
)


def shared_drive_lock_path(dest: str) -> str:
    """Map the torrent dest and the HTTP dest to the same drive-level lock file."""
    path = os.path.abspath(dest)
    if os.path.basename(path) == "DEF CON" and os.path.basename(os.path.dirname(path)) == "cons":
        path = os.path.dirname(os.path.dirname(path))
    return os.path.join(path, ".infocon_scraper.lock")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def acquire_lock(lock_path: str, force: bool = False) -> bool:
    """Create an exclusive PID lock on the drive root. Returns False if another active sync holds it."""
    if os.path.exists(lock_path):
        try:
            with open(lock_path, encoding="utf-8") as f:
                text = f.read().strip()
            pid = int(text) if text else 0
        except (ValueError, OSError):
            pid = 0
        if pid and _pid_alive(pid) and not force:
            return False
        try:
            os.remove(lock_path)
        except OSError:
            pass
    try:
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class TorrentSettings:
    max_active: int
    connections: int
    listen_interface: str
    poll_seconds: int
    seed_time: int
    enable_dht: bool
    enable_pex: bool
    enable_lsd: bool
    request_timeout: int
    retries: int
    retry_delay: int
    stalled_minutes: int = 30
    discovery_workers: int = 8
    max_defcon_active: int = 1
    torrent_order: str = "newest"
    discovery_retries: int = 3
    discovery_retry_delay: float = 5.0
    # Per-torrent detail lines printed each poll. The status block used to list
    # every incomplete torrent every cycle: at 293 torrents on a 10s poll that
    # is ~2,500 log lines a minute, and it dominated the run log.
    status_lines: int = 10
    # Seeding. The archive asks contributors to help grow it, and a rebuilt
    # drive is a fully-populated seed, so the tool gives back by default.
    # upload_slots is how many peers may download from us at once; rate_limit
    # caps the bandwidth that costs, in KiB/s (0 = unlimited).
    seed_upload_slots: int = 4
    seed_rate_limit_kib: int = 0
    max_seeding: int = 20
    # Minutes between resume-data checkpoints. Without resume data every restart
    # re-hash-checks the whole set before a single byte can transfer.
    resume_save_minutes: int = 5


@dataclass(frozen=True)
class TorrentSpec:
    name: str
    url: str
    save_path: str


def build_libtorrent_session(settings: TorrentSettings) -> lt.session:
    """Create a libtorrent session using the settings keys supported by libtorrent 2.1.x.

    This runtime does not expose an `enable_pex` session setting, so peer exchange
    remains under libtorrent's default behavior rather than being forced via a
    non-existent setting key.
    """
    limit = settings.max_active if settings.max_active > 0 else -1
    seeds = settings.max_seeding if settings.max_seeding > 0 else -1
    session_settings = lt.default_settings()
    session_settings["listen_interfaces"] = settings.listen_interface
    session_settings["active_downloads"] = limit
    session_settings["active_seeds"] = seeds
    # active_limit covers downloads and seeds together. Leaving it at the
    # download limit would make every seeding torrent compete for a download
    # slot, so a drive that finished an archive could not share it.
    session_settings["active_limit"] = -1 if (limit < 0 or seeds < 0) else limit + seeds
    session_settings["connections_limit"] = settings.connections
    # How many peers may pull from us at once, and at what cost.
    if "unchoke_slots_limit" in session_settings:
        session_settings["unchoke_slots_limit"] = max(0, settings.seed_upload_slots)
    if "upload_rate_limit" in session_settings:
        session_settings["upload_rate_limit"] = max(0, settings.seed_rate_limit_kib) * 1024
    # Resume-data checkpoints arrive as alerts, so the storage category has to
    # be enabled for them to be delivered at all.
    if "alert_mask" in session_settings:
        session_settings["alert_mask"] = int(
            lt.alert.category_t.status_notification | lt.alert.category_t.storage_notification
            | lt.alert.category_t.error_notification
        )

    if "enable_dht" in session_settings:
        session_settings["enable_dht"] = bool(settings.enable_dht)
    if "enable_lsd" in session_settings:
        session_settings["enable_lsd"] = bool(settings.enable_lsd)
    if "enable_pex" in session_settings:
        session_settings["enable_pex"] = bool(settings.enable_pex)

    params = lt.session_params()
    params.settings = session_settings
    return lt.session(params)


def _run_curl(command: list[str], url: str) -> subprocess.CompletedProcess:
    """Run curl under the shared per-host concurrency budget.

    Discovery listings compete with the HTTP scraper for the same hosts, so both
    honour one budget rather than each assuming it has the connection cap to
    itself.
    """
    from infocon_scraper import host_semaphore

    sem = host_semaphore(url)
    if sem:
        sem.acquire()
    try:
        return subprocess.run(command, capture_output=True, text=False)
    finally:
        if sem:
            sem.release()


def curl_text(url: str, settings: TorrentSettings, timeout: int | None = None,
              retries: int | None = None) -> str:
    proc = _run_curl(
        ["curl", "-sS", "-A", USER_AGENT, "--retry", str(settings.retries if retries is None else retries),
         "--retry-delay", str(settings.retry_delay), "--retry-all-errors",
         "--max-time", str(timeout or settings.request_timeout), "-L", "--fail", url],
        url,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"curl failed ({proc.returncode}) for {url}: "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout.decode("utf-8", errors="replace")


def curl_download(url: str, local_path: str, settings: TorrentSettings) -> None:
    proc = _run_curl(
        ["curl", "-sS", "-A", USER_AGENT, "--retry", str(settings.retries),
         "--retry-delay", str(settings.retry_delay), "--retry-all-errors",
         "--max-time", str(settings.request_timeout), "-L", "--fail",
         "-o", local_path, url],
        url,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"curl failed ({proc.returncode}) for {url}: "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )


def _safe_segment(name: str) -> str | None:
    """A listing name usable as one path segment, or None if it would escape.

    Names come from the remote listing and are joined onto save_path, so an
    absolute or traversing name would place torrent content outside the drive.
    """
    if not name or name in (os.curdir, os.pardir):
        return None
    if os.path.isabs(name) or "/" in name or "\\" in name:
        return None
    if os.path.basename(name) != name:
        return None
    return name


def _listing_entries(html: str) -> list[tuple[str, str, bool]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="list")
    if table is None:
        return []
    entries: list[tuple[str, str, bool]] = []
    for link in (table.find("tbody") or table).find_all("a"):
        href = link.get("href", "")
        if href in ("../", "./") or href.startswith(("?", "http://", "https://")):
            continue
        name = _safe_segment(unquote(href.rstrip("/")))
        if name is None:
            print(f"Ignoring listing entry {href!r}: not a usable path segment")
            continue
        entries.append((href, name, href.endswith("/")))
    return entries


def discover_torrents_recursive(roots: list[tuple[str, str]], settings: TorrentSettings,
                                include_mirrors: bool = False,
                                include_rainbow_tables: bool = False,
                                checkpoint_path: str | None = None) -> list[TorrentSpec]:
    """Recursively find torrent files, excluding mirrors and rainbow tables by default."""
    visited: set[str] = set()
    found: dict[str, tuple[int, TorrentSpec]] = {}
    pending_items = list(roots)
    fingerprint = {"roots": [list(root) for root in roots], "include_mirrors": include_mirrors,
                   "include_rainbow_tables": include_rainbow_tables}
    if checkpoint_path:
        try:
            with open(checkpoint_path, encoding="utf-8") as stream:
                checkpoint = json.load(stream)
            if checkpoint.get("fingerprint") == fingerprint:
                visited = set(checkpoint.get("visited", []))
                pending_items = [tuple(item) for item in checkpoint.get("pending", [])]
                found = {key: (value["version"], TorrentSpec(**value["spec"]))
                         for key, value in checkpoint.get("found", {}).items()}
                print(
                    f"Resuming torrent discovery checkpoint: {len(visited)} directories visited, "
                    f"{len(pending_items)} saved pending URLs (not a total), {len(found)} known candidates."
                )
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError, KeyError):
            pass
    print(f"Recursive torrent discovery started across {len(roots)} root(s); mirrors={'included' if include_mirrors else 'excluded'}, rainbow tables={'included' if include_rainbow_tables else 'excluded'}.")
    def fetch_listing(item: tuple[str, str]) -> tuple[str, str, list[tuple[str, str, bool]]]:
        dir_url, save_path = item
        return dir_url, save_path, _listing_entries(curl_text(
            dir_url, settings, timeout=min(settings.request_timeout, 30), retries=0
        ))

    pending: dict = {}
    retry_queue: list[tuple[float, int, tuple[str, str], int]] = []
    retry_sequence = 0
    scheduled_retries = 0
    last_checkpoint = time.time()

    def save_checkpoint() -> None:
        if not checkpoint_path:
            return
        payload = {
            "fingerprint": fingerprint,
            "visited": sorted(visited),
            "pending": (
                [list(item) for item in pending_items]
                + [list(item) for item, _ in pending.values()]
                + [list(item) for _, _, item, _ in retry_queue]
            ),
            "found": {key: {"version": version, "spec": spec.__dict__}
                      for key, (version, spec) in found.items()},
        }
        os.makedirs(os.path.dirname(checkpoint_path) or ".", exist_ok=True)
        temporary = checkpoint_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
        os.replace(temporary, checkpoint_path)

    # Honour the configured worker count. It used to be silently clamped to 4
    # while the CLI, the wizard and the README all advertised 8.
    discovery_pool_size = max(1, settings.discovery_workers)
    print(f"Torrent discovery using {discovery_pool_size} listing worker(s).")
    with ThreadPoolExecutor(max_workers=discovery_pool_size) as pool:
        for item in pending_items:
            pending[pool.submit(fetch_listing, item)] = (item, 0)
        pending_items = []
        while pending or retry_queue:
            now = time.time()
            while retry_queue and retry_queue[0][0] <= now:
                _, _, item, failed_attempts = heapq.heappop(retry_queue)
                pending[pool.submit(fetch_listing, item)] = (item, failed_attempts)
            if not pending:
                time.sleep(min(0.25, max(0.0, retry_queue[0][0] - time.time())))
                continue

            retry_wait = None
            if retry_queue:
                retry_wait = max(0.0, retry_queue[0][0] - time.time())
            done, _ = wait(pending, timeout=retry_wait, return_when=FIRST_COMPLETED)
            for future in done:
                item, failed_attempts = pending.pop(future)
                dir_url, save_path = item
                if dir_url in visited:
                    continue
                try:
                    _, _, entries = future.result()
                except RuntimeError as exc:
                    if failed_attempts < settings.discovery_retries:
                        failed_attempts += 1
                        delay = settings.discovery_retry_delay * (2 ** (failed_attempts - 1))
                        retry_sequence += 1
                        heapq.heappush(retry_queue, (time.time() + delay, retry_sequence, item, failed_attempts))
                        scheduled_retries += 1
                        print(
                            f"Torrent listing failed for {dir_url}; retry "
                            f"{failed_attempts}/{settings.discovery_retries} in {delay:.1f}s: {exc}"
                        )
                    else:
                        print(f"Skipping torrent listing {dir_url} after {settings.discovery_retries} retries: {exc}")
                    continue
                visited.add(dir_url)
                if len(visited) % 100 == 0:
                    print(f"Torrent discovery: scanned {len(visited)} directories, found {len(found)} torrent candidates...")
                for href, name, is_dir in entries:
                    child_url = urljoin(dir_url, href)
                    if is_dir:
                        if not include_mirrors and child_url.lower().rstrip("/").endswith("/mirrors"):
                            continue
                        if not include_rainbow_tables and child_url.lower().rstrip("/").endswith("/rainbow%20tables"):
                            continue
                        if child_url not in visited:
                            child_item = (child_url, os.path.join(save_path, name))
                            pending[pool.submit(fetch_listing, child_item)] = (child_item, 0)
                        continue
                    if not name.lower().endswith(".torrent"):
                        continue
                    stem = re.sub(r"\.torrent$", "", name, flags=re.IGNORECASE)
                    stem = re.sub(r"\s+v\d+(?:\s*-\s*infocon\.org)?$", "", stem, flags=re.IGNORECASE)
                    candidate = TorrentSpec(name=stem, url=child_url, save_path=save_path)
                    version_match = re.search(r"\bv(\d+)\b", name, re.IGNORECASE)
                    version = int(version_match.group(1)) if version_match else 0
                    logical = torrent_logical_name(stem)
                    key = f"defcon/{logical}" if logical.startswith("def con") else f"{save_path}/{logical}".lower()
                    previous = found.get(key)
                    category = "DEF CON" if re.search(r"\bdef con\b", stem, re.IGNORECASE) else "InfoCon"
                    print(f"Torrent candidate [{category}]: {child_url} -> {save_path} (v{version or 1})")
                    if previous is None or version > previous[0] or (
                        version == previous[0]
                        and torrent_source_priority(candidate) < torrent_source_priority(previous[1])
                    ):
                        found[key] = (version, candidate)
                        if previous is not None:
                            print(f"Torrent candidate replaced: {previous[1].url} -> {child_url}")
                    else:
                        print(f"Torrent candidate ignored: duplicate/lower-priority {child_url}")
                    if len(found) % 10 == 0:
                        print(f"Eligible torrent candidates so far: {len(found)}")
                if time.time() - last_checkpoint >= 30:
                    last_checkpoint = time.time()
                    save_checkpoint()
    save_checkpoint()
    specs = sorted((spec for _, spec in found.values()), key=lambda spec: torrent_order_key(spec.name, settings.torrent_order))
    print(
        f"Recursive torrent discovery complete: scanned {len(visited)} directories, found {len(specs)} torrent files, "
        f"scheduled {scheduled_retries} retries."
    )
    for spec in specs:
        print(f"Torrent selected for download: {spec.name} -> {spec.url} -> {spec.save_path}")
    return specs


def discover_torrents_indexed(roots: list[tuple[str, str]], settings: TorrentSettings,
                              index_path: str, index_ttl_hours: int,
                              include_mirrors: bool = False,
                              include_rainbow_tables: bool = False,
                              checkpoint_path: str | None = None) -> list[TorrentSpec]:
    """Reuse the online torrent inventory until its fingerprinted TTL expires."""
    fingerprint = {
        "roots": [list(root) for root in roots],
        "include_mirrors": include_mirrors,
        "include_rainbow_tables": include_rainbow_tables,
    }
    try:
        with open(index_path, encoding="utf-8") as stream:
            cached = json.load(stream)
        age_hours = (time.time() - float(cached["scanned_at"])) / 3600
        if age_hours <= index_ttl_hours and cached.get("fingerprint") == fingerprint:
            specs = [TorrentSpec(**item) for item in cached["torrents"]]
            print(f"Loaded torrent inventory index: {len(specs)} entries, age {age_hours:.1f} hours.")
            return specs
        print(f"Torrent inventory index expired or changed; rescanning online (age {age_hours:.1f} hours).")
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        print("No usable torrent inventory index; scanning the online tree.")

    specs = discover_torrents_recursive(
        roots, settings, include_mirrors=include_mirrors,
        include_rainbow_tables=include_rainbow_tables,
        checkpoint_path=checkpoint_path,
    )
    os.makedirs(os.path.dirname(index_path) or ".", exist_ok=True)
    temporary = index_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump({
            "scanned_at": time.time(),
            "fingerprint": fingerprint,
            "torrents": [spec.__dict__ for spec in specs],
        }, stream, indent=2, sort_keys=True)
    os.replace(temporary, index_path)
    print(f"Saved torrent inventory index: {len(specs)} entries at {index_path}.")
    return specs


def _status_is_paused(status) -> bool:
    """True when libtorrent reports the torrent as paused.

    `torrent_status.flags` is the authoritative source in libtorrent 2.x; the
    legacy `.paused` attribute is used as a fallback so the helper keeps working
    on builds that do not expose flags.
    """
    flags = getattr(status, "flags", None)
    if flags is not None:
        try:
            return bool(flags & lt.torrent_flags.paused)
        except TypeError:
            pass
    return bool(getattr(status, "paused", False))


def _wants_to_download(name: str, status, selected_names: set[str]) -> bool:
    """True when the scheduler has actually asked this torrent to download.

    A torrent that we paused ourselves - because it is queued behind
    `--max-active` - always reports zero peers and zero download rate. Treating
    that as a stall would hand every queued archive to the HTTP fallback within
    `stalled_minutes` of checking finishing, which is exactly what a run with
    `max_active < len(torrents)` used to do.
    """
    if name not in selected_names:
        return False
    return not _status_is_paused(status)


def resume_file_path(resume_dir: str, spec: TorrentSpec) -> str:
    """Where a torrent's fast-resume checkpoint lives."""
    key = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{spec.save_path}_{spec.name}")
    return os.path.join(resume_dir, f"{key}.resume")


def load_add_params(spec: TorrentSpec, info, resume_dir: str):
    """add_torrent_params for *spec*, reusing fast-resume data when present.

    Resume data is what lets a restart skip re-hashing content that was already
    verified. Without it every run re-checks the full set - terabytes on a
    populated drive - before it can transfer anything.
    """
    path = resume_file_path(resume_dir, spec)
    try:
        with open(path, "rb") as stream:
            atp = lt.read_resume_data(stream.read())
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        if not isinstance(exc, FileNotFoundError):
            print(f"Ignoring unusable resume data for {spec.name}: {exc}")
        atp = lt.add_torrent_params()
    atp.ti = info
    atp.save_path = spec.save_path
    return atp


def handle_key(handle) -> str:
    """Stable identity for a torrent handle, used to route resume alerts.

    The alert only carries the torrent's own internal name, which is not the
    label we track it under, so the info hash is what ties an alert back to the
    file it should be written to.
    """
    try:
        return str(handle.info_hash())
    except RuntimeError:
        return ""


def save_resume_alerts(session, resume_targets: dict[str, str]) -> int:
    """Persist resume-data alerts the session has produced. Returns a count."""
    saved = 0
    for alert in session.pop_alerts():
        if isinstance(alert, lt.save_resume_data_alert):
            path = resume_targets.get(handle_key(alert.handle))
            if not path:
                continue
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                temporary = path + ".tmp"
                with open(temporary, "wb") as stream:
                    stream.write(lt.write_resume_data_buf(alert.params))
                os.replace(temporary, path)
                saved += 1
            except OSError as exc:
                print(f"Could not write resume data to {path}: {exc}")
        elif isinstance(alert, lt.save_resume_data_failed_alert):
            # Routine: libtorrent declines when nothing changed since last save.
            pass
    return saved


def checkpoint_before_exit(session, handles: dict, resume_targets: dict[str, str],
                           request: Callable[[], None], timeout: float = 30.0) -> None:
    """Flush fast-resume data for every torrent before the process exits.

    Skipping this is what made a restart re-hash the whole set, so it is worth
    a bounded wait even on an interrupted run.
    """
    if not handles:
        return
    for handle in handles.values():
        try:
            handle.pause()
        except RuntimeError:
            continue
    request()
    deadline = time.time() + timeout
    saved = 0
    while time.time() < deadline and saved < len(handles):
        saved += save_resume_alerts(session, resume_targets)
        if saved >= len(handles):
            break
        time.sleep(0.2)
    print(f"Checkpointed fast-resume data for {saved} of {len(handles)} torrent(s).")


def torrent_priority(name: str) -> tuple[int, int, str]:
    """Sort numbered DEF CON archives newest first, then non-numbered items."""
    match = re.search(r"\b(?:DEF CON|DC)\s+(\d+)\b", name, re.IGNORECASE)
    if match:
        return (0, -int(match.group(1)), name.lower())
    return (1, 0, name.lower())


def torrent_order_key(name: str, order: str) -> tuple[int, int, str]:
    if order == "oldest":
        match = re.search(r"\b(?:DEF CON|DC)\s+(\d+)\b", name, re.IGNORECASE)
        if match:
            return (0, int(match.group(1)), name.lower())
        return (1, 0, name.lower())
    return torrent_priority(name)


def torrent_content_folder(name: str) -> str:
    """The published folder name an archive torrent corresponds to.

    infocon.org publishes 'cons/2600 archive v1 - infocon.org.torrent' alongside
    the 'cons/2600/' folder holding the same content, so stripping the archive
    suffix maps a torrent back to the directory HTTP would fetch it from.
    Case is preserved because that folder name is used to build URLs and paths.
    """
    normalized = re.sub(r"\s+archive(?:\s+v\d+)?(?:\s+-\s+infocon\.org)?$", "", name, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", normalized).strip()


def torrent_logical_name(name: str) -> str:
    """Normalize archive labels so duplicate published torrent locations collapse."""
    return torrent_content_folder(name).lower()


def torrent_source_priority(spec: TorrentSpec) -> int:
    """Prefer media.defcon.org for every DEF CON archive."""
    if "media.defcon.org" in spec.url.lower():
        return 0
    path = spec.save_path.replace(os.sep, "/").lower().rstrip("/")
    if path.endswith("/cons/def con/def con torrents"):
        return 1
    if path.endswith("/cons/def con"):
        return 2
    return 2


def _merge_infocon_candidates(
    available: list[TorrentSpec],
    infocon_candidates: list[dict],
) -> list[TorrentSpec]:
    """Merge raw infocon.org torrent candidates from scan_infocon_tree into
    an existing TorrentSpec list discovered from other roots (e.g. media.defcon.org).

    Candidates whose logical name already appears in *available* are dropped so
    the media.defcon.org entry always wins.  Within the new candidates the
    highest version wins.  *available* is returned extended in-place.
    """
    # Logical keys already covered by the media.defcon.org scan
    covered: set[str] = set()
    for spec in available:
        logical = torrent_logical_name(spec.name)
        key = (f"defcon/{logical}" if logical.startswith("def con")
               else f"{spec.save_path}/{logical}".lower())
        covered.add(key)

    # Pick best version for each logical name within the infocon.org candidates
    best: dict[str, tuple[int, dict]] = {}
    for cand in infocon_candidates:
        raw_name = cand.get("name", "")
        if not raw_name.lower().endswith(".torrent"):
            continue
        vm = re.search(r"\bv(\d+)\b", raw_name, re.IGNORECASE)
        version = int(vm.group(1)) if vm else 0
        stem = re.sub(r"\.torrent$", "", raw_name, flags=re.IGNORECASE)
        stem = re.sub(r"\s+v\d+(?:\s*-\s*infocon\.org)?$", "", stem, flags=re.IGNORECASE).strip()
        logical = torrent_logical_name(stem)
        save_path = cand.get("save_path", "")
        key = (f"defcon/{logical}" if logical.startswith("def con")
               else f"{save_path}/{logical}".lower())
        if key in covered:
            continue
        prev = best.get(key)
        if prev is None or version > prev[0]:
            best[key] = (version, {"stem": stem, "url": cand["url"], "save_path": save_path})

    for key, (_, info) in best.items():
        available.append(TorrentSpec(name=info["stem"], url=info["url"], save_path=info["save_path"]))
        covered.add(key)

    return available


def fetch_all(dest: str, torrents_dir: str, only: list[str] | None,
              settings: TorrentSettings, ready_event: threading.Event | None = None,
              skip: list[str] | None = None,
              stalled_callback: Callable[[TorrentSpec], None] | None = None,
              torrent_roots: list[tuple[str, str]] | None = None,
              include_mirrors: bool = False,
              include_rainbow_tables: bool = False,
              defcon_only: list[str] | None = None,
              discovery_event: threading.Event | None = None,
              index_path: str | None = None,
              index_ttl_hours: int = 168,
              checkpoint_path: str | None = None,
              infocon_candidates: list[dict] | None = None,
              stop_event: threading.Event | None = None) -> int:
    """Discover and download torrents.

    When *infocon_candidates* is supplied (from scan_infocon_tree in
    infocon_scraper.py), the infocon.org root is excluded from the network
    discovery so only media.defcon.org/DEF CON Torrents/ is scanned; the
    pre-discovered infocon.org torrent entries are merged in afterwards.  This
    eliminates the duplicate infocon.org directory traversal.
    """
    os.makedirs(dest, exist_ok=True)
    os.makedirs(torrents_dir, exist_ok=True)

    roots = torrent_roots or [(TORRENTS_DIR_URL, dest)]

    if infocon_candidates is not None:
        # infocon.org already traversed by the shared scan; only hit media.defcon.org
        effective_roots = [(url, sp) for url, sp in roots if "infocon.org" not in url.lower()]
        # If no non-infocon root remains, fall back to the full list so at least
        # media.defcon.org is always queried for authoritative DEF CON torrents.
        if not effective_roots:
            effective_roots = roots
        # Checkpointing covers the full infocon.org tree, which is now external;
        # skip it for the media.defcon.org-only scan to avoid a stale-fingerprint hit.
        effective_checkpoint = None
    else:
        effective_roots = roots
        effective_checkpoint = checkpoint_path

    available = discover_torrents_indexed(
        effective_roots, settings,
        index_path or os.path.join(os.path.expanduser("~"), ".cache", "infocon-scraper", "torrent-index.json"),
        index_ttl_hours,
        include_mirrors=include_mirrors,
        include_rainbow_tables=include_rainbow_tables,
        checkpoint_path=effective_checkpoint,
    )

    if infocon_candidates is not None:
        available = _merge_infocon_candidates(available, infocon_candidates)

    if only:
        filters = [f.lower() for f in only]
        available = [spec for spec in available if any(f in spec.name.lower() for f in filters)]
    if defcon_only:
        filters = [f.lower() for f in defcon_only]
        available = [
            spec for spec in available
            if not re.search(r"\bdef con\b", spec.name, re.IGNORECASE)
            or any(f in spec.name.lower() for f in filters)
        ]
    if skip:
        filters = [f.lower() for f in skip]
        available = [spec for spec in available if not any(f in spec.name.lower() for f in filters)]
    if not available:
        print("No matching torrents found.")
        if discovery_event is not None:
            discovery_event.set()
        return 1

    defcon_count = sum(1 for spec in available if re.search(r"\bdef con\b", spec.name, re.IGNORECASE))
    print(f"Found {len(available)} torrents to fetch/verify ({defcon_count} DEF CON, {len(available) - defcon_count} other InfoCon torrents).")
    if discovery_event is not None:
        print("Online torrent inventory complete; HTTP may now begin without bypassing discovered torrent coverage.")
        discovery_event.set()
    # libtorrent auto-manages torrents and by default only actively downloads a
    # few at once (active_downloads=3); the rest sit queued with 0 peers. Raise
    # the limits so the whole set downloads in parallel. max_active <= 0 means
    # unlimited (-1 in libtorrent).
    ses = build_libtorrent_session(settings)
    handles = {}
    specs_by_name = {spec.name: spec for spec in available}
    completed_at: dict[str, float] = {}
    no_activity_since: dict[str, float] = {}
    stalled_names: set[str] = set()
    # Names the scheduler currently wants downloading. Only these accrue quiet
    # time towards the stalled-torrent fallback: every other torrent is paused
    # by us on purpose and would otherwise report zero peers / zero rate and be
    # handed to HTTP within `stalled_minutes` of checking finishing.
    selected_names: set[str] = set()
    # Completed torrents currently sharing back to the swarm.
    seeding_names: set[str] = set()

    resume_dir = os.path.join(torrents_dir, "resume")
    resume_targets: dict[str, str] = {}
    queued_specs = deque(sorted(available,
                                key=lambda item: torrent_order_key(item.name, settings.torrent_order)))
    total_specs = len(queued_specs)
    skipped_specs = 0
    # Every torrent added at once means every torrent hash-checks at once. On a
    # populated multi-terabyte drive that is a self-inflicted disk storm before
    # any transfer can start, so torrents are admitted a window at a time.
    add_window = max(4, (settings.max_active if settings.max_active > 0 else 8) * 2)

    def add_next_torrent() -> bool:
        nonlocal skipped_specs
        while queued_specs:
            spec = queued_specs.popleft()
            cache_name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{spec.save_path}_{spec.name}")
            torrent_path = os.path.join(torrents_dir, f"{cache_name}.torrent")
            if not os.path.exists(torrent_path):
                print(f"Fetching torrent metadata for {spec.name} ...")
                try:
                    curl_download(spec.url, torrent_path, settings)
                except RuntimeError as exc:
                    print(f"Skipping {spec.name}: could not fetch torrent ({exc})")
                    skipped_specs += 1
                    continue
            try:
                info = lt.torrent_info(torrent_path)
            except RuntimeError as exc:
                print(f"Skipping {spec.name}: could not load torrent ({exc})")
                skipped_specs += 1
                continue
            handle = ses.add_torrent(load_add_params(spec, info, resume_dir))
            handles[spec.name] = handle
            resume_targets[handle_key(handle)] = resume_file_path(resume_dir, spec)
            resumed = os.path.exists(resume_file_path(resume_dir, spec))
            print(f"Added {spec.name}: {info.total_size() / 1e9:.2f} GB, {info.num_files()} files"
                  f"{' (fast resume)' if resumed else ''}")
            return True
        return False

    def top_up_torrents(finished: set[str]) -> None:
        """Keep a bounded window of torrents admitted to the session."""
        while queued_specs and len(handles) - len(finished) < add_window:
            if not add_next_torrent():
                break

    def request_resume_checkpoints() -> None:
        for name, handle in handles.items():
            if name in stalled_names:
                continue
            try:
                if handle.is_valid() and handle.status().has_metadata:
                    handle.save_resume_data(lt.save_resume_flags_t.only_if_modified)
            except RuntimeError:
                continue

    top_up_torrents(set())
    print(f"Admitted {len(handles)} of {total_specs} torrent(s); the rest join as slots free up.")
    print("Verifying/downloading... (Ctrl+C to stop; already-correct files are skipped automatically)")
    try:
        checking_complete = False
        scheduler_locked = False
        last_resume_request = time.time()
        announced_complete = False
        while True:
            done = 0
            active = []
            checking = False
            upload_rate = 0
            peers_served = 0
            seeding_names.clear()
            for name, h in handles.items():
                if name in stalled_names:
                    continue
                s = h.status()
                if s.state in (
                    lt.torrent_status.queued_for_checking,
                    lt.torrent_status.checking_files,
                    lt.torrent_status.checking_resume_data,
                ):
                    checking = True
                if s.progress >= 1.0 and s.state in (
                    lt.torrent_status.seeding,
                    lt.torrent_status.finished,
                    lt.torrent_status.paused,
                ):
                    done += 1
                    completed_at.setdefault(name, time.time())
                    seeded_for = time.time() - completed_at[name]
                    if settings.seed_time == 0:
                        if s.state != lt.torrent_status.paused:
                            h.pause()
                    elif settings.seed_time > 0 and seeded_for >= settings.seed_time * 60:
                        if s.state != lt.torrent_status.paused:
                            print(f"Seeded {name} for {settings.seed_time} minutes; pausing.")
                            h.pause()
                    else:
                        # Still within the seed window, or seeding until stopped.
                        if s.state == lt.torrent_status.paused:
                            h.resume()
                        seeding_names.add(name)
                        upload_rate += s.upload_rate
                        peers_served += s.num_peers
                else:
                    if not _wants_to_download(name, s, selected_names):
                        # Queued behind the active-torrent limit (or paused for
                        # another reason): it is not stalled, it was never given
                        # a chance to run, so its quiet timer must not accrue.
                        no_activity_since.pop(name, None)
                    elif checking_complete and stalled_callback is not None:
                        if s.num_peers == 0 and s.download_rate == 0:
                            no_activity_since.setdefault(name, time.time())
                            quiet_for = time.time() - no_activity_since[name]
                            if quiet_for >= settings.stalled_minutes * 60:
                                stalled_names.add(name)
                                h.pause()
                                print(f"Stalled after {quiet_for / 60:.0f} minutes; handing {name} back to HTTP.")
                                stalled_callback(specs_by_name[name])
                                continue
                        else:
                            no_activity_since.pop(name, None)
                    active.append((s.download_rate, s.progress, s.num_peers, str(s.state), name))
            # Highest download rate first; ties break newest-DEF-CON-first instead of
            # alphabetically (plain string sort would put "DEF CON 2" ahead of "19").
            active.sort(key=lambda item: (-item[0], -item[1], -item[2], torrent_order_key(item[4], settings.torrent_order)))
            if checking_complete and not scheduler_locked:
                for handle in handles.values():
                    handle.unset_flags(lt.torrent_flags.auto_managed)
                scheduler_locked = True
            if checking_complete:
                incomplete = [
                    (name, handle) for name, handle in handles.items()
                    if name not in stalled_names and handle.status().progress < 1.0
                ]
                defcon = sorted(
                    (item for item in incomplete if re.search(r"\bdef con\b", item[0], re.IGNORECASE)),
                    key=lambda item: torrent_order_key(item[0], settings.torrent_order)
                )
                other = sorted(
                    (item for item in incomplete if not re.search(r"\bdef con\b", item[0], re.IGNORECASE)),
                    key=lambda item: item[0].lower()
                )
                active_limit = settings.max_active if settings.max_active > 0 else len(incomplete)
                selected = defcon[:settings.max_defcon_active] + other[:max(0, active_limit - settings.max_defcon_active)]
                selected_names = {name for name, _ in selected}
                for name, handle in incomplete:
                    if name in selected_names:
                        handle.resume()
                    else:
                        handle.pause()
                        no_activity_since.pop(name, None)
            total_rate = sum(a[0] for a in active)
            downloading = sum(1 for a in active if a[0] > 0)
            waiting = len(queued_specs)
            seeding_note = ""
            if settings.seed_time != 0 and seeding_names:
                seeding_note = (f" | seeding {len(seeding_names)} to {peers_served} peer(s) "
                                f"at {upload_rate / 1e6:.2f} MB/s up")
            print(f"--- {done}/{total_specs - skipped_specs} complete | {total_rate / 1e6:6.2f} MB/s total | "
                  f"{downloading} active, {len(active) - downloading} queued/idle, "
                  f"{waiting} not yet admitted{seeding_note} ---")
            # Detail lines are capped: printing every incomplete torrent each
            # poll produced thousands of log lines a minute and buried
            # everything else in the run log.
            shown = active[:max(0, settings.status_lines)]
            for rate, progress, peers, state, name in shown:
                print(f"{name}: {progress * 100:5.1f}%  down {rate / 1e6:6.2f} MB/s  "
                      f"peers {peers}  state {state}")
            if len(active) > len(shown):
                states: dict[str, int] = {}
                for _, _, _, state, _ in active[len(shown):]:
                    states[state] = states.get(state, 0) + 1
                summary = ", ".join(f"{count} {state}" for state, count in sorted(states.items()))
                print(f"... and {len(active) - len(shown)} more ({summary})")

            if ready_event is not None and not checking_complete and not checking:
                checking_complete = True
                print("Initial torrent file checking complete; HTTP sync may proceed in parallel.")
                ready_event.set()

            finished_names = {name for name in handles if name in completed_at} | stalled_names
            top_up_torrents(finished_names)

            saved = save_resume_alerts(ses, resume_targets)
            if saved:
                print(f"Saved fast-resume data for {saved} torrent(s).")
            if time.time() - last_resume_request >= settings.resume_save_minutes * 60:
                last_resume_request = time.time()
                request_resume_checkpoints()

            target_count = len(handles) - len(stalled_names)
            if done == target_count and not queued_specs:
                stopping = stop_event is not None and stop_event.is_set()
                if settings.seed_time < 0 and not stopping:
                    if not announced_complete:
                        announced_complete = True
                        print("All requested DEF CON items fully downloaded and verified; "
                              "seeding until stopped (--seed-time 0 to exit on completion).")
                else:
                    print("All requested DEF CON items fully downloaded and verified.")
                    break
            if stop_event is not None and stop_event.is_set():
                print("Stop requested; pausing torrents and checkpointing resume data.")
                break
            time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        print("Interrupted - already-verified pieces are safe; re-run to resume/continue.")
        checkpoint_before_exit(ses, handles, resume_targets, request_resume_checkpoints)
        return 1
    finally:
        if ready_event is not None and not ready_event.is_set():
            ready_event.set()
    checkpoint_before_exit(ses, handles, resume_targets, request_resume_checkpoints)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch/verify DEF CON archives via BitTorrent v2 (libtorrent).")
    parser.add_argument("--dest", required=True, help="Destination cons/DEF CON directory")
    parser.add_argument("--force", action="store_true",
                         help="Override the shared drive-level lock and run anyway")
    parser.add_argument("--only", default=None,
                         help="Comma-separated substrings to restrict which items are fetched "
                              "(default: all available torrents)")
    parser.add_argument("--defcon-only", default=None,
                        help="Comma-separated DEF CON numbers to fetch when recursive roots include other sources")
    parser.add_argument("--include-mirrors", action="store_true",
                        help="Recursively search infocon.org/mirrors too; disabled by default because it is enormous")
    parser.add_argument("--include-rainbow-tables", action="store_true",
                        help="Recursively search infocon.org/rainbow tables too; disabled by default because it is multi-terabyte")
    parser.add_argument("--discovery-workers", type=int, default=8,
                        help="Concurrent recursive torrent listing workers (default: 8)")
    parser.add_argument("--torrent-index", default=os.environ.get(
        "INFOCON_TORRENT_INDEX",
        os.path.join(os.path.expanduser("~"), ".cache", "infocon-scraper", "torrent-index.json")),
                        help="Persistent online torrent inventory index path")
    parser.add_argument("--torrent-index-ttl-hours", type=int, default=168,
                        help="Hours before the online torrent inventory is rescanned (default: 168 / 7 days)")
    parser.add_argument("--torrent-discovery-checkpoint", default=os.environ.get(
        "INFOCON_TORRENT_DISCOVERY_CHECKPOINT",
        os.path.join(os.path.expanduser("~"), ".cache", "infocon-scraper", "torrent-discovery-checkpoint.json")),
                        help="Resumable recursive discovery checkpoint path")
    parser.add_argument("--skip", default=None,
                        help="Comma-separated substrings to skip, useful for archives arriving separately")
    parser.add_argument("--torrents-dir", default=DEFAULT_TORRENTS_CACHE,
                         help=f"Where to cache .torrent files (default: {DEFAULT_TORRENTS_CACHE})")
    parser.add_argument("--max-active", type=int, default=4,
                         help="Maximum simultaneous active torrents; newest are added first; 0 means unlimited (default: 4)")
    parser.add_argument("--max-defcon-active", type=int, default=1,
                         help="Maximum simultaneous DEF CON torrents; remaining active slots prefer other InfoCon torrents (default: 1)")
    parser.add_argument("--torrent-order", choices=("newest", "oldest"), default="newest",
                        help="Torrent discovery/priority order (default: newest)")
    parser.add_argument("--connections", type=int, default=800,
                         help="Global libtorrent connection limit (default: 800)")
    parser.add_argument("--listen-interface", default="0.0.0.0:6881",
                         help="Listening address and port, e.g. 0.0.0.0:6881 or 192.0.2.10:51413")
    parser.add_argument("--poll-seconds", type=int, default=10,
                         help="Progress reporting interval (default: 10)")
    parser.add_argument("--status-lines", type=int, default=10,
                        help="Per-torrent detail lines printed each poll; the rest are summarised "
                             "(default: 10)")
    parser.add_argument("--resume-save-minutes", type=int, default=5,
                        help="Minutes between fast-resume checkpoints, which let a restart skip "
                             "re-hashing verified content (default: 5)")
    parser.add_argument("--seed-time", type=int, default=60,
                         help="Minutes to keep sharing each archive after it completes. "
                              "0 stops seeding the moment a torrent finishes; a negative value "
                              "seeds until the run is stopped (default: 60). The archive is "
                              "community-hosted and asks contributors to help it grow, so a "
                              "rebuilt drive shares back by default.")
    parser.add_argument("--seed-upload-slots", type=int, default=4,
                        help="How many peers may download from you at once (default: 4)")
    parser.add_argument("--seed-rate-limit", type=int, default=0,
                        help="Upload cap in KiB/s while seeding; 0 is unlimited (default: 0)")
    parser.add_argument("--max-seeding", type=int, default=20,
                        help="Maximum archives seeding at once (default: 20)")
    parser.add_argument("--stalled-minutes", type=int, default=30,
                        help="Minutes with zero peers and zero download rate before combined mode hands an item to HTTP (default: 30)")
    parser.add_argument("--no-dht", action="store_true", help="Disable the distributed hash table")
    parser.add_argument("--no-pex", action="store_true", help="Disable peer exchange")
    parser.add_argument("--no-lsd", action="store_true", help="Disable local peer discovery")
    parser.add_argument("--request-timeout", type=int, default=120,
                         help="curl metadata request timeout in seconds (default: 120)")
    parser.add_argument("--retries", type=int, default=3,
                         help="curl retry count for metadata (default: 3)")
    parser.add_argument("--retry-delay", type=int, default=3,
                         help="Seconds between metadata retries (default: 3)")
    args = parser.parse_args()

    lock_path = shared_drive_lock_path(args.dest)
    if not acquire_lock(lock_path, force=args.force):
        print(f"Another sync appears to be running for the same drive root (lock: {lock_path}). Use --force to override.")
        return 2

    only = [f.strip() for f in args.only.split(",") if f.strip()] if args.only else None
    skip = [f.strip() for f in args.skip.split(",") if f.strip()] if args.skip else None
    defcon_only = [f.strip() for f in args.defcon_only.split(",") if f.strip()] if args.defcon_only else None
    settings = TorrentSettings(
        max_active=args.max_active,
        connections=args.connections,
        listen_interface=args.listen_interface,
        poll_seconds=max(1, args.poll_seconds),
        seed_time=args.seed_time,
        enable_dht=not args.no_dht,
        enable_pex=not args.no_pex,
        enable_lsd=not args.no_lsd,
        request_timeout=max(1, args.request_timeout),
        retries=max(0, args.retries),
        retry_delay=max(0, args.retry_delay),
        stalled_minutes=max(1, args.stalled_minutes),
        discovery_workers=max(1, args.discovery_workers),
        max_defcon_active=max(0, args.max_defcon_active),
        torrent_order=args.torrent_order,
        status_lines=max(0, args.status_lines),
        resume_save_minutes=max(1, args.resume_save_minutes),
        seed_upload_slots=max(0, args.seed_upload_slots),
        seed_rate_limit_kib=max(0, args.seed_rate_limit),
        max_seeding=max(1, args.max_seeding),
    )
    return fetch_all(args.dest, args.torrents_dir, only, settings, skip=skip,
                     include_mirrors=args.include_mirrors,
                     include_rainbow_tables=args.include_rainbow_tables,
                     defcon_only=defcon_only,
                     index_path=args.torrent_index,
                     index_ttl_hours=max(0, args.torrent_index_ttl_hours),
                     checkpoint_path=args.torrent_discovery_checkpoint)


if __name__ == "__main__":
    sys.exit(main())

