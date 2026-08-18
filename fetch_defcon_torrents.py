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
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import libtorrent as lt

TORRENTS_DIR_URL = "https://media.defcon.org/DEF%20CON%20Torrents/"
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
            with open(lock_path, "r", encoding="utf-8") as f:
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


def build_libtorrent_session(settings: TorrentSettings) -> lt.session:
    """Create a libtorrent session using the settings keys supported by libtorrent 2.1.x.

    This runtime does not expose an `enable_pex` session setting, so peer exchange
    remains under libtorrent's default behavior rather than being forced via a
    non-existent setting key.
    """
    limit = settings.max_active if settings.max_active > 0 else -1
    session_settings = lt.default_settings()
    session_settings["listen_interfaces"] = settings.listen_interface
    session_settings["active_downloads"] = limit
    session_settings["active_seeds"] = limit
    session_settings["active_limit"] = limit
    session_settings["connections_limit"] = settings.connections

    if "enable_dht" in session_settings:
        session_settings["enable_dht"] = bool(settings.enable_dht)
    if "enable_lsd" in session_settings:
        session_settings["enable_lsd"] = bool(settings.enable_lsd)
    if "enable_pex" in session_settings:
        session_settings["enable_pex"] = bool(settings.enable_pex)

    params = lt.session_params()
    params.settings = session_settings
    return lt.session(params)


def curl_text(url: str, settings: TorrentSettings) -> str:
    proc = subprocess.run(
        ["curl", "-sS", "-A", USER_AGENT, "--retry", str(settings.retries),
         "--retry-delay", str(settings.retry_delay), "--retry-all-errors",
         "--max-time", str(settings.request_timeout), "-L", "--fail", url],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed ({proc.returncode}) for {url}: {proc.stderr.strip()}")
    return proc.stdout


def curl_download(url: str, local_path: str, settings: TorrentSettings) -> None:
    proc = subprocess.run(
        ["curl", "-sS", "-A", USER_AGENT, "--retry", str(settings.retries),
         "--retry-delay", str(settings.retry_delay), "--retry-all-errors",
         "--max-time", str(settings.request_timeout), "-L", "--fail",
         "-o", local_path, url],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed ({proc.returncode}) for {url}: {proc.stderr.strip()}")


def discover_torrents(dir_url: str, settings: TorrentSettings) -> dict[str, str]:
    """Return {base_name: href} picking the highest 'vN' version per base name."""
    html = curl_text(dir_url, settings)
    hrefs = re.findall(r'href="([^"]+\.torrent)"', html)
    best: dict[str, tuple[int, str]] = {}
    for href in hrefs:
        name = href.replace("%20", " ")
        m = re.match(r"^(.*) v(\d+)\.torrent$", name)
        if not m:
            continue
        base, version = m.group(1), int(m.group(2))
        if base not in best or version > best[base][0]:
            best[base] = (version, href)
    return {base: href for base, (_, href) in best.items()}


def fetch_all(dest: str, torrents_dir: str, only: list[str] | None,
              settings: TorrentSettings, ready_event: threading.Event | None = None) -> int:
    os.makedirs(dest, exist_ok=True)
    os.makedirs(torrents_dir, exist_ok=True)

    available = discover_torrents(TORRENTS_DIR_URL, settings)
    if only:
        filters = [f.lower() for f in only]
        available = {name: href for name, href in available.items()
                     if any(f in name.lower() for f in filters)}
    if not available:
        print("No matching torrents found.")
        return 1

    print(f"Found {len(available)} torrents to fetch/verify.")
    # libtorrent auto-manages torrents and by default only actively downloads a
    # few at once (active_downloads=3); the rest sit queued with 0 peers. Raise
    # the limits so the whole set downloads in parallel. max_active <= 0 means
    # unlimited (-1 in libtorrent).
    ses = build_libtorrent_session(settings)
    handles = {}
    completed_at: dict[str, float] = {}

    for name, href in sorted(available.items()):
        torrent_url = TORRENTS_DIR_URL + href
        torrent_path = os.path.join(torrents_dir, f"{name}.torrent")
        if not os.path.exists(torrent_path):
            print(f"Fetching torrent metadata for {name} ...")
            curl_download(torrent_url, torrent_path, settings)
        try:
            info = lt.torrent_info(torrent_path)
        except RuntimeError as exc:
            print(f"Skipping {name}: could not load torrent ({exc})")
            continue
        atp = lt.add_torrent_params()
        atp.ti = info
        atp.save_path = dest
        h = ses.add_torrent(atp)
        handles[name] = h
        print(f"Added {name}: {info.total_size() / 1e9:.2f} GB, {info.num_files()} files")

    print("Verifying/downloading... (Ctrl+C to stop; already-correct files are skipped automatically)")
    try:
        checking_complete = False
        while True:
            done = 0
            active = []
            checking = False
            for name, h in handles.items():
                s = h.status()
                if s.state in (lt.torrent_status.checking_files, lt.torrent_status.checking_resume_data):
                    checking = True
                if s.progress >= 1.0 and s.state in (
                    lt.torrent_status.seeding,
                    lt.torrent_status.finished,
                    lt.torrent_status.paused,
                ):
                    done += 1
                    completed_at.setdefault(name, time.time())
                    if settings.seed_time == 0 and s.state != lt.torrent_status.paused:
                        h.pause()
                    elif settings.seed_time > 0 and time.time() - completed_at[name] >= settings.seed_time * 60:
                        h.pause()
                else:
                    active.append((s.download_rate, s.progress, s.num_peers, str(s.state), name))
            active.sort(reverse=True)  # highest download rate first
            total_rate = sum(a[0] for a in active)
            downloading = sum(1 for a in active if a[0] > 0)
            print(f"--- {done}/{len(handles)} complete | {total_rate / 1e6:6.2f} MB/s total | "
                  f"{downloading} active, {len(active) - downloading} queued/idle ---")
            for rate, progress, peers, state, name in active[:12]:
                print(f"{name}: {progress * 100:5.1f}%  down {rate / 1e6:6.2f} MB/s  "
                      f"peers {peers}  state {state}")
            if ready_event is not None and not checking_complete and not checking:
                checking_complete = True
                print("Initial torrent file checking complete; HTTP sync may proceed in parallel.")
                ready_event.set()
            if done == len(handles):
                print("All requested DEF CON items fully downloaded and verified.")
                break
            time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        print("Interrupted - already-verified pieces are safe; re-run to resume/continue.")
        return 1
    finally:
        if ready_event is not None and not ready_event.is_set():
            ready_event.set()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch/verify DEF CON archives via BitTorrent v2 (libtorrent).")
    parser.add_argument("--dest", required=True, help="Destination cons/DEF CON directory")
    parser.add_argument("--force", action="store_true",
                         help="Override the shared drive-level lock and run anyway")
    parser.add_argument("--only", default=None,
                         help="Comma-separated substrings to restrict which items are fetched "
                              "(default: all available torrents)")
    parser.add_argument("--torrents-dir", default=DEFAULT_TORRENTS_CACHE,
                         help=f"Where to cache .torrent files (default: {DEFAULT_TORRENTS_CACHE})")
    parser.add_argument("--max-active", type=int, default=8,
                         help="Maximum simultaneous active torrents; 0 means unlimited (default: 8)")
    parser.add_argument("--connections", type=int, default=800,
                         help="Global libtorrent connection limit (default: 800)")
    parser.add_argument("--listen-interface", default="0.0.0.0:6881",
                         help="Listening address and port, e.g. 0.0.0.0:6881 or 192.0.2.10:51413")
    parser.add_argument("--poll-seconds", type=int, default=10,
                         help="Progress reporting interval (default: 10)")
    parser.add_argument("--seed-time", type=int, default=0,
                         help="Minutes to seed after completion; 0 disables seeding")
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
    settings = TorrentSettings(
        max_active=args.max_active,
        connections=args.connections,
        listen_interface=args.listen_interface,
        poll_seconds=max(1, args.poll_seconds),
        seed_time=max(0, args.seed_time),
        enable_dht=not args.no_dht,
        enable_pex=not args.no_pex,
        enable_lsd=not args.no_lsd,
        request_timeout=max(1, args.request_timeout),
        retries=max(0, args.retries),
        retry_delay=max(0, args.retry_delay),
    )
    return fetch_all(args.dest, args.torrents_dir, only, settings)


if __name__ == "__main__":
    sys.exit(main())

