#!/usr/bin/env python3
"""
infocon_scraper.py

Incrementally mirrors the entire InfoCon.org archive (https://infocon.org/)
onto a local drive: every conference under cons/ (DEF CON and BSides first,
everything else after), plus documentaries/, podcasts/, rainbow tables/,
skills/, word lists/ - including the .torrent files published alongside
each section. mirrors/ (vx underground, textfiles.com, etc.) is excluded
by default since it's huge enough to fill a drive on its own; pass
--only-top mirrors to include it explicitly.

Crawling and downloading happen at the same time (as soon as a file is
discovered it's queued for download), so priority sections don't get stuck
waiting behind slow-to-list trees elsewhere on the site.

Designed to *update* an existing InfoCon drive rather than re-download
everything: it recursively crawls the site, and for each remote file
decides whether to download based on:
  - missing locally                      -> download
  - size mismatch vs remote              -> re-download (likely truncated/corrupt)
  - size matches but no known-good hash  -> hash it and record a baseline
  - size matches and hash matches a      -> skip (already verified)
    previously recorded baseline
  - size matches but hash differs from   -> re-download (corruption detected,
    a previously recorded baseline          e.g. from a bad duplication run)

A manifest (JSON) of {relative_path: {size, sha256, url, verified}} is kept
next to the destination so re-runs are fast and corruption can be detected
over time.

Usage:
    python infocon_scraper.py --dest "/media/chiefgyk3d/infocon.org DC30"

    # Re-verify everything already downloaded (hash every file):
    python infocon_scraper.py --dest "/media/chiefgyk3d/infocon.org DC30" --verify-all

    # See what would happen without downloading anything:
    python infocon_scraper.py --dest "/media/chiefgyk3d/infocon.org DC30" --dry-run

    # Restrict to a subset of conferences (substring match, case-insensitive):
    python infocon_scraper.py --dest "/media/chiefgyk3d/infocon.org DC30" \\
        --only-cons "DEF CON,BSides"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

# infocon.org's WAF resets connections made with Python's TLS stack
# (requests/urllib3/urllib all get RemoteDisconnected), but the system
# `curl` binary works fine. All HTTP is therefore shelled out to curl.
DEFAULT_ROOT_URL = "https://infocon.org/"
# media.defcon.org is DEF CON's own media server - it's the authoritative
# source for DEF CON content and often has files infocon.org's mirror
# doesn't (yet). Its root *is* the DEF CON archive (year folders directly).
DEFCON_MEDIA_ROOT_URL = "https://media.defcon.org/"
# Top-level sections at the infocon.org site root, besides cons/ (which is
# expanded into individual conference folders so DEF CON/BSides can be
# prioritized).
TOP_LEVEL_SECTIONS = [
    "documentaries",
    "podcasts",
    "rainbow tables",
    "skills",
    "word lists",
]
# mirrors/ (vx underground malware samples, textfiles.com, etc.) is huge
# enough on its own to fill a drive and is excluded by default. Pass
# --only-top mirrors explicitly to include it.
ALL_TOP_LEVEL_SECTIONS = TOP_LEVEL_SECTIONS + ["mirrors"]
# Priority conference names under cons/: DEF CON first, then any conference
# whose name contains "bsides" (case-insensitive), then the rest.
USER_AGENT = "InfoConDriveSync/1.0 (personal archive sync tool)"
CHUNK_SIZE = 1 << 20  # 1 MiB

log = logging.getLogger("infocon_scraper")


@dataclass
class RemoteFile:
    url: str
    rel_path: str


class CurlError(RuntimeError):
    pass


# media.defcon.org appears to throttle/drop connections once too many are
# open at once (crawl + download workers combined easily exceed a dozen).
# Cap concurrent requests per host to avoid connection-timeout errors that
# silently drop whole subfolders from the crawl.
HOST_CONCURRENCY_LIMITS = {"media.defcon.org": 6}
_host_semaphores: dict[str, threading.Semaphore] = {}
_host_semaphores_lock = threading.Lock()


def _host_semaphore(url: str) -> threading.Semaphore | None:
    host = urlparse(url).netloc
    limit = HOST_CONCURRENCY_LIMITS.get(host)
    if not limit:
        return None
    with _host_semaphores_lock:
        sem = _host_semaphores.get(host)
        if sem is None:
            sem = threading.Semaphore(limit)
            _host_semaphores[host] = sem
        return sem


def run_curl(args: list[str], timeout: int, url: str | None = None) -> subprocess.CompletedProcess:
    sem = _host_semaphore(url) if url else None
    if sem:
        sem.acquire()
    try:
        return subprocess.run(
            ["curl", "-sS", "-A", USER_AGENT, "--retry", "3", "--retry-delay", "3",
             "--retry-all-errors", "--max-time", str(timeout)] + args,
            capture_output=True, text=False,
        )
    finally:
        if sem:
            sem.release()


def curl_get_text(url: str, timeout: int = 30) -> str:
    proc = run_curl(["-L", "--fail", url], timeout, url=url)
    if proc.returncode != 0:
        raise CurlError(f"curl failed ({proc.returncode}) for {url}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode("utf-8", errors="replace")


def curl_head_size(url: str, timeout: int = 30) -> int | None:
    proc = run_curl(["-I", "-L", "--fail", url], timeout, url=url)
    if proc.returncode != 0:
        raise CurlError(f"curl HEAD failed ({proc.returncode}) for {url}: {proc.stderr.decode(errors='replace').strip()}")
    size = None
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        if line.lower().startswith("content-length:"):
            size = int(line.split(":", 1)[1].strip())
    return size


def curl_download(url: str, local_path: str, resume: bool, timeout: int = 3600) -> None:
    args = ["-L", "--fail", "-o", local_path]
    if resume:
        args += ["-C", "-"]
    args += [url]
    proc = run_curl(args, timeout, url=url)
    if proc.returncode != 0:
        raise CurlError(f"curl download failed ({proc.returncode}) for {url}: {proc.stderr.decode(errors='replace').strip()}")


def list_directory(url: str) -> list[dict]:
    """Parse one fancyindex directory listing page into entries."""
    html = curl_get_text(url)
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="list")
    entries: list[dict] = []
    if table is None:
        return entries
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        link = tr.find("a")
        if link is None:
            continue
        href = link.get("href", "")
        if href in ("../", "./") or href.startswith("?") or href.startswith(("http://", "https://")):
            continue
        entries.append({
            "href": href,
            "name": unquote(href.rstrip("/")),
            "is_dir": href.endswith("/"),
        })
    return entries


def run_sync(roots: list[tuple[str, str]], dest_root: str, manifest: "Manifest", crawl_workers: int,
             download_workers: int, verify_all: bool, dry_run: bool, stop_requested: threading.Event,
             initial_files: list[RemoteFile] | None = None,
             max_pending_downloads: int | None = None) -> dict[str, int]:
    """Crawl and download at the same time: as soon as a file is discovered it's
    handed to the download pool immediately, instead of waiting for the entire
    site to be crawled first. This is what lets priority roots (DEF CON,
    BSides - submitted first) start downloading right away, without being
    blocked behind huge, slow-to-list trees (e.g. vx underground's hundreds of
    thousands of malware-sample folders) discovered from later roots.
    """
    counts: dict[str, int] = {}
    discovered = 0
    completed = 0
    ready_files = deque(initial_files or [])
    pending_downloads = 0
    download_limit = max_pending_downloads or max(download_workers * 4, download_workers)

    with ThreadPoolExecutor(max_workers=crawl_workers) as crawl_pool, \
            ThreadPoolExecutor(max_workers=download_workers) as dl_pool:
        pending: dict = {}
        for url, rel in roots:
            pending[crawl_pool.submit(list_directory, url)] = ("list", (url, rel))
        while pending:
            if stop_requested.is_set():
                break
            done, _ = wait(list(pending.keys()), return_when=FIRST_COMPLETED)
            for fut in done:
                kind, payload = pending[fut]
                if kind == "list" and pending_downloads >= download_limit:
                    continue
                pending.pop(fut)
                if kind == "list":
                    url, rel = payload
                    try:
                        entries = fut.result()
                    except Exception as exc:  # noqa: BLE001 - a single bad listing must not kill the whole crawl
                        log.error("Failed to list %s: %s", url, exc)
                        continue
                    log.info("Listed %s (%d entries)", url, len(entries))
                    for entry in entries:
                        child_url = urljoin(url, entry["href"])
                        child_rel = os.path.join(rel, entry["name"]) if rel else entry["name"]
                        if entry["is_dir"]:
                            pending[crawl_pool.submit(list_directory, child_url)] = ("list", (child_url, child_rel))
                        else:
                            item = RemoteFile(url=child_url, rel_path=child_rel)
                            discovered += 1
                            ready_files.append(item)
                else:
                    item = payload
                    pending_downloads -= 1
                    try:
                        result = fut.result()
                    except Exception as exc:  # noqa: BLE001 - log and continue
                        log.error("Unexpected error for %s: %s", item.rel_path, exc)
                        result = "error"
                    counts[result] = counts.get(result, 0) + 1
                    completed += 1
                    if completed % 25 == 0:
                        log.info("Progress: %d files completed (%d discovered so far)", completed, discovered)

            while ready_files and pending_downloads < download_limit:
                item = ready_files.popleft()
                pending[dl_pool.submit(sync_file, item, dest_root, manifest, verify_all, dry_run)] = \
                    ("sync", item)
                pending_downloads += 1

            if not pending and ready_files:
                log.error("Stopping with %d discovered files still queued", len(ready_files))
                break

    log.info("Progress: %d files completed (%d discovered total)", completed, discovered)
    return counts


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(block)
    return h.hexdigest()


class Manifest:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, dict] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read manifest %s (%s), starting fresh", path, exc)

    def get(self, rel_path: str) -> dict | None:
        with self.lock:
            return self.data.get(rel_path)

    def set(self, rel_path: str, entry: dict) -> None:
        with self.lock:
            self.data[rel_path] = entry

    def save(self) -> None:
        with self.lock:
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)


ARCHIVE_EXTENSIONS = {".rar", ".zip", ".7z"}


def is_unpacked_archive_duplicate(local_path: str) -> bool:
    """True if local_path is a .rar/.zip/.7z file whose exact unpacked-folder
    equivalent already exists (e.g. 'DEF CON 21 pictures.rar' next to an
    existing 'DEF CON 21 pictures/' folder). media.defcon.org publishes both
    forms for the same content; no need to fetch the compressed copy too."""
    base, ext = os.path.splitext(local_path)
    return ext.lower() in ARCHIVE_EXTENSIONS and os.path.isdir(base)


def sync_file(item: RemoteFile, dest_root: str, manifest: Manifest,
              verify_all: bool, dry_run: bool) -> str:
    local_path = os.path.join(dest_root, item.rel_path)
    rel = item.rel_path

    if is_unpacked_archive_duplicate(local_path):
        log.info("Skipping %s (already have the unpacked folder)", rel)
        return "skip-duplicate-archive"

    try:
        remote_size = curl_head_size(item.url)
    except CurlError as exc:
        log.error("HEAD failed for %s: %s", item.url, exc)
        return "error"

    exists = os.path.exists(local_path)
    local_size = os.path.getsize(local_path) if exists else 0

    if exists and remote_size is not None and local_size == remote_size:
        entry = manifest.get(rel)
        if entry and entry.get("size") == remote_size and not verify_all:
            return "skip-known-good"
        digest = sha256_file(local_path)
        if entry and entry.get("sha256") == digest:
            manifest.set(rel, {"size": remote_size, "sha256": digest, "url": item.url,
                                "verified": time.time()})
            return "skip-verified"
        if entry and entry.get("sha256") != digest:
            log.warning("Corruption detected for %s (hash mismatch), re-downloading", rel)
        else:
            manifest.set(rel, {"size": remote_size, "sha256": digest, "url": item.url,
                                "verified": time.time()})
            return "baseline-recorded"

    if dry_run:
        action = "would-resume" if exists and remote_size and local_size < remote_size else "would-download"
        log.info("[dry-run] %s -> %s", action, rel)
        return action

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    resume = bool(exists and remote_size and local_size < remote_size)
    try:
        curl_download(item.url, local_path, resume=resume)
    except CurlError as exc:
        log.error("Download failed for %s: %s", item.url, exc)
        return "error"

    final_size = os.path.getsize(local_path)
    if remote_size is not None and final_size != remote_size:
        log.error("Size mismatch after download for %s (got %d, expected %d)", rel, final_size, remote_size)
        return "error"

    digest = sha256_file(local_path)
    manifest.set(rel, {"size": final_size, "sha256": digest, "url": item.url, "verified": time.time()})
    log.info("Downloaded %s (%d bytes)", rel, final_size)
    return "downloaded"


def conf_priority_rank(name: str) -> int:
    """0 = DEF CON (any year folder, e.g. 'DEF CON' or 'DEF CON 34'),
    1 = BSides variants, 2 = everything else."""
    lowered = name.lower()
    if lowered == "def con" or lowered.startswith("def con "):
        return 0
    if "bsides" in lowered:
        return 1
    return 2


def discover_cons_folders(root_url: str) -> list[str]:
    entries = list_directory(urljoin(root_url, "cons/"))
    return [e["name"] for e in entries if e["is_dir"]]


def build_infocon_roots(root_url: str, only_cons: list[str] | None,
                        only_top: list[str] | None,
                        only_mirrors: list[str] | None) -> list[tuple[str, str]]:
    """Build (url, rel_prefix) roots for infocon.org, ordered DEF CON, then BSides, then the rest."""
    cons_names = discover_cons_folders(root_url)
    if only_cons:
        filters = [f.lower() for f in only_cons]
        cons_names = [n for n in cons_names if any(f in n.lower() for f in filters)]
    cons_names.sort(key=lambda n: (conf_priority_rank(n), n.lower()))

    roots = [(urljoin(root_url, f"cons/{quote(n)}/"), f"cons/{n}") for n in cons_names]

    sections = ALL_TOP_LEVEL_SECTIONS if only_top else TOP_LEVEL_SECTIONS
    if only_top:
        filters = [f.lower() for f in only_top]
        sections = [s for s in sections if any(f in s.lower() for f in filters)]
    roots += [(urljoin(root_url, f"{quote(s)}/"), s) for s in sections]
    if only_mirrors:
        mirror_filters = [f.lower() for f in only_mirrors]
        mirror_entries = discover_top_level_folders(urljoin(root_url, "mirrors/"))
        roots.extend(
            (urljoin(root_url, f"mirrors/{quote(name)}/"), f"mirrors/{name}")
            for name in mirror_entries
            if any(f in name.lower() for f in mirror_filters)
        )
    return roots


def discover_top_level_folders(root_url: str) -> list[str]:
    entries = list_directory(root_url)
    return [e["name"] for e in entries if e["is_dir"]]


def discover_mirror_files(root_url: str, filters: list[str] | None) -> list[RemoteFile]:
    entries = list_directory(urljoin(root_url, "mirrors/"))
    if not filters:
        return []
    lowered = [f.lower() for f in filters]
    return [
        RemoteFile(url=urljoin(urljoin(root_url, "mirrors/"), entry["href"]),
                   rel_path=os.path.join("mirrors", entry["name"]))
        for entry in entries
        if not entry["is_dir"]
        and not entry["name"].lower().endswith(".torrent")
        and any(f in entry["name"].lower() for f in lowered)
    ]


def build_mirror_roots(root_url: str, filters: list[str] | None) -> list[tuple[str, str]]:
    if not filters:
        return [(urljoin(root_url, "mirrors/"), "mirrors")]
    lowered = [f.lower() for f in filters]
    return [
        (urljoin(root_url, f"mirrors/{quote(name)}/"), f"mirrors/{name}")
        for name in discover_top_level_folders(urljoin(root_url, "mirrors/"))
        if any(f in name.lower() for f in lowered)
    ]


def build_defcon_media_roots(root_url: str, skip_names: set[str] | None = None) -> list[tuple[str, str]]:
    """Expand media.defcon.org's root into per-folder roots, targeting the same
    cons/DEF CON/ location infocon.org uses so both sources land in one place.
    Per-file skip/verify logic in sync_file() already avoids re-downloading
    years that are complete locally - a folder-existence pre-filter would
    wrongly skip years that are only partially downloaded so far."""
    skip = {n.lower() for n in (skip_names or set())}
    names = [n for n in discover_top_level_folders(root_url) if n.lower() not in skip]
    names.sort(key=lambda n: (conf_priority_rank(n), n.lower()))
    return [(urljoin(root_url, f"{quote(n)}/"), f"cons/DEF CON/{n}") for n in names]


def build_roots(sources: list[str], infocon_root: str, defcon_media_root: str, defcon_media_skip: set[str] | None,
                 only_cons: list[str] | None, only_top: list[str] | None,
                 only_mirrors: list[str] | None) -> list[tuple[str, str]]:
    roots: list[tuple[str, str]] = []
    # media.defcon.org goes first: it's the highest-priority content (DEF CON)
    # and all its roots are submitted for crawling up front, so it shouldn't
    # sit behind ~240 infocon.org conference folders in the submission queue.
    if "defcon-media" in sources:
        roots += build_defcon_media_roots(defcon_media_root, defcon_media_skip)
    if "infocon" in sources:
        roots += build_infocon_roots(infocon_root, only_cons, only_top, only_mirrors)
    elif "mirrors" in sources:
        roots += build_mirror_roots(infocon_root, only_mirrors)
    return roots


def find_torrent_files(dir_url: str, name_contains: str) -> list[dict]:
    """List .torrent files in a directory whose name contains `name_contains` (case-insensitive)."""
    entries = list_directory(dir_url)
    needle = name_contains.lower()
    return [e for e in entries if not e["is_dir"] and e["name"].endswith(".torrent") and needle in e["name"].lower()]


def pick_latest_torrent(entries: list[dict]) -> dict | None:
    """Pick the highest 'vN' version among matching torrent entries (e.g. v2 over v1)."""
    def version_of(name: str) -> int:
        m = re.search(r"v(\d+)", name, re.IGNORECASE)
        return int(m.group(1)) if m else 0
    return max(entries, key=lambda e: version_of(e["name"])) if entries else None


def fetch_torrent_file(dir_url: str, name_contains: str, dest_dir: str) -> str | None:
    """Download the latest matching .torrent file into dest_dir, returning its local path."""
    chosen = pick_latest_torrent(find_torrent_files(dir_url, name_contains))
    if not chosen:
        return None
    torrent_url = urljoin(dir_url, chosen["href"])
    os.makedirs(dest_dir, exist_ok=True)
    local_path = os.path.join(dest_dir, chosen["name"])
    curl_download(torrent_url, local_path, resume=False, timeout=120)
    return local_path


def run_aria2c_torrent(torrent_path: str, download_dir: str) -> int:
    """Hand a .torrent file off to aria2c to fetch via BitTorrent (resumable, piece-hash verified)."""
    aria2c = shutil.which("aria2c")
    if not aria2c:
        log.error(
            "aria2c is not installed. Install it (e.g. 'sudo apt install aria2') then run:\n"
            "  aria2c --dir=%s --continue=true --file-allocation=none --seed-time=0 %s",
            download_dir, torrent_path,
        )
        return 1
    os.makedirs(download_dir, exist_ok=True)
    log.info("Starting aria2c torrent download into %s ...", download_dir)
    proc = subprocess.run([
        aria2c, "--dir", download_dir, "--continue=true",
        "--file-allocation=none", "--seed-time=0", torrent_path,
    ])
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror the InfoCon.org archive to a local drive.")
    parser.add_argument("--dest", required=True, help="Local destination root (e.g. your InfoCon drive mount point)")
    parser.add_argument("--base-url", default=DEFAULT_ROOT_URL, help="Root URL of the InfoCon.org site")
    parser.add_argument("--defcon-media-url", default=DEFCON_MEDIA_ROOT_URL,
                         help="Root URL of media.defcon.org (DEF CON's own authoritative media server)")
    parser.add_argument("--sources", default="infocon,defcon-media",
                        help="Comma-separated sources to sync: 'infocon' (everything on infocon.org), "
                            "'mirrors' (only infocon.org/mirrors), and/or 'defcon-media' "
                            "(media.defcon.org, DEF CON only). Default: both infocon and defcon-media.")
    parser.add_argument("--only-cons", default=None,
                         help="Comma-separated substrings to restrict which infocon.org cons/ conferences are "
                              "synced (default: all conferences)")
    parser.add_argument("--only-top", default=None,
                         help="Comma-separated substrings to restrict which infocon.org top-level sections "
                              "besides cons/ are synced, e.g. 'documentaries,podcasts' (default: all sections)")
    parser.add_argument("--only-mirrors", default=None,
                         help="Comma-separated mirror name filters, e.g. 'cryptome,textfiles'; downloads only "
                              "matching collections under infocon.org/mirrors/")
    parser.add_argument("--defcon-media-skip", default=None,
                         help="Comma-separated media.defcon.org folder names to skip entirely, e.g. "
                              "'DEF CON 30,DEF CON 31' when fetching those years via BitTorrent instead")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent download workers")
    parser.add_argument("--max-pending-downloads", type=int, default=None,
                         help="Maximum queued/in-flight file downloads (default: workers * 4)")
    parser.add_argument("--crawl-workers", type=int, default=16, help="Concurrent directory-listing workers")
    parser.add_argument("--verify-all", action="store_true",
                         help="Re-hash every existing file in scope, not just new ones")
    parser.add_argument("--dry-run", action="store_true", help="List actions without downloading")
    parser.add_argument("--manifest", default=None, help="Path to manifest JSON (default: <dest>/.infocon_manifest.json)")
    parser.add_argument("--log-file", default=None, help="Path to log file (default: <dest>/infocon_scraper.log)")
    parser.add_argument("--list-torrents", metavar="NAME",
                         help="Instead of syncing, list available .torrent files under infocon.org/cons/ whose "
                              "name contains NAME (e.g. 'DEF CON') and exit")
    parser.add_argument("--fetch-torrent", metavar="NAME",
                         help="Instead of syncing, download the latest matching .torrent under infocon.org/cons/ "
                              "(e.g. 'DEF CON') and hand it to aria2c to fetch into --dest/torrents-download/NAME, "
                              "then exit. Note: aria2c only supports BitTorrent v1; for DEF CON's v2 torrents on "
                              "media.defcon.org use fetch_defcon_torrents.py instead")
    args = parser.parse_args()

    if args.list_torrents:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        cons_url = urljoin(args.base_url, "cons/")
        for entry in find_torrent_files(cons_url, args.list_torrents):
            print(urljoin(cons_url, entry["href"]))
        return 0

    if args.fetch_torrent:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        cons_url = urljoin(args.base_url, "cons/")
        torrent_path = fetch_torrent_file(cons_url, args.fetch_torrent, os.path.join(args.dest, "torrents"))
        if not torrent_path:
            log.error("No .torrent file found matching %r under %s", args.fetch_torrent, cons_url)
            return 1
        log.info("Fetched torrent: %s", torrent_path)
        download_dir = os.path.join(args.dest, "torrents-download", args.fetch_torrent)
        return run_aria2c_torrent(torrent_path, download_dir)

    manifest_path = args.manifest or os.path.join(args.dest, ".infocon_manifest.json")
    log_file = args.log_file or os.path.join(args.dest, "infocon_scraper.log")
    os.makedirs(args.dest, exist_ok=True)

    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError as exc:
        print(f"Warning: could not open log file {log_file}: {exc}", file=sys.stderr)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    only_cons = [f.strip() for f in args.only_cons.split(",") if f.strip()] if args.only_cons else None
    only_top = [f.strip() for f in args.only_top.split(",") if f.strip()] if args.only_top else None
    only_mirrors = [f.strip() for f in args.only_mirrors.split(",") if f.strip()] if args.only_mirrors else None
    defcon_media_skip = {f.strip() for f in args.defcon_media_skip.split(",") if f.strip()} \
        if args.defcon_media_skip else None
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    log.info("Discovering content from sources: %s ...", ", ".join(sources))
    roots = build_roots(sources, args.base_url, args.defcon_media_url, defcon_media_skip,
                        only_cons, only_top, only_mirrors)
    log.info("Target sections (priority order): %s", ", ".join(rel for _, rel in roots))
    manifest = Manifest(manifest_path)

    stop_requested = threading.Event()

    def handle_sigint(signum, frame):
        log.warning("Interrupt received, finishing in-flight downloads and saving manifest...")
        stop_requested.set()

    signal.signal(signal.SIGINT, handle_sigint)

    initial_files = discover_mirror_files(args.base_url, only_mirrors) \
        if "infocon" in sources or "mirrors" in sources else []

    try:
        counts = run_sync(roots, args.dest, manifest, args.crawl_workers, args.workers,
                           args.verify_all, args.dry_run, stop_requested, initial_files,
                           args.max_pending_downloads)
    finally:
        manifest.save()

    log.info("Done. Summary: %s", json.dumps(counts, indent=2))
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
