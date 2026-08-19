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
from datetime import datetime
from typing import Callable
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


@dataclass
class RunConfig:
    retries: int = 4
    download_timeout: int = 3600
    min_free_bytes: int = 1 << 30  # keep at least 1 GiB free
    manifest_save_every: int = 200
    content_order: str = "newest"


# Populated once in main() before any worker starts; read-only during a run.
RUN = RunConfig()


def shared_drive_lock_path(dest: str) -> str:
    """Maps both the HTTP root and the DEF CON torrent dest to the same drive-level lock file."""
    path = os.path.abspath(dest)
    if os.path.basename(path) == "DEF CON" and os.path.basename(os.path.dirname(path)) == "cons":
        path = os.path.dirname(os.path.dirname(path))
    return os.path.join(path, ".infocon_scraper.lock")


def human_bytes(n: float) -> str:
    """Compact human-readable byte count, e.g. 12.3 MB."""
    step = 1000.0
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < step or unit == "PB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= step
    return f"{n:.1f} PB"


def format_duration(seconds: float) -> str:
    """Compact H:MM:SS / M:SS duration, or '--' when unknown."""
    if seconds <= 0 or seconds != seconds or seconds == float("inf"):
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def progress_bar(fraction: float, width: int = 24) -> str:
    """ASCII progress bar like [########----------------]."""
    fraction = 0.0 if fraction < 0 else (1.0 if fraction > 1 else fraction)
    filled = int(round(fraction * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


class ProgressStats:
    """Thread-safe aggregate counters shared between the download workers and
    the status reporter thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.start = time.time()
        self.discovered = 0
        self.completed = 0
        self.downloaded_files = 0
        self.downloaded_bytes = 0
        self.skipped = 0
        self.errors = 0
        self.active = 0

    def add_discovered(self, n: int = 1) -> None:
        with self.lock:
            self.discovered += n

    def download_started(self) -> None:
        with self.lock:
            self.active += 1

    def download_finished(self, status: str, nbytes: int) -> None:
        with self.lock:
            self.active = max(0, self.active - 1)
            self.completed += 1
            if status == "downloaded":
                self.downloaded_files += 1
                self.downloaded_bytes += nbytes
            elif status.startswith("error"):
                self.errors += 1
            elif status.startswith("skip") or status == "baseline-recorded":
                self.skipped += 1

    def snapshot(self) -> dict[str, float]:
        with self.lock:
            return {
                "discovered": self.discovered,
                "completed": self.completed,
                "downloaded_files": self.downloaded_files,
                "downloaded_bytes": self.downloaded_bytes,
                "skipped": self.skipped,
                "errors": self.errors,
                "active": self.active,
            }


class StatusReporter(threading.Thread):
    """Periodically emits an aggregate progress line (bar, counts, speed, ETA).
    Renders a live single-line bar when stdout is a TTY, and always logs a
    snapshot line so `tail -f` on the log shows progress too."""

    def __init__(self, stats: ProgressStats, interval: float = 10.0) -> None:
        super().__init__(daemon=True)
        self.stats = stats
        self.interval = max(1.0, interval)
        self._stop_event = threading.Event()
        self.is_tty = sys.stdout.isatty()
        self._last_time = stats.start
        self._last_bytes = 0

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            self._emit()
        self._emit(final=True)

    def _emit(self, final: bool = False) -> None:
        s = self.stats.snapshot()
        now = time.time()
        elapsed = now - self.stats.start
        window = now - self._last_time
        cur_rate = (s["downloaded_bytes"] - self._last_bytes) / window if window > 0 else 0.0
        avg_rate = s["downloaded_bytes"] / elapsed if elapsed > 0 else 0.0
        self._last_time = now
        self._last_bytes = s["downloaded_bytes"]

        discovered = s["discovered"]
        completed = s["completed"]
        fraction = (completed / discovered) if discovered else 0.0
        comp_rate = completed / elapsed if elapsed > 0 else 0.0
        remaining = max(discovered - completed, 0)
        eta = remaining / comp_rate if comp_rate > 0 else 0.0

        line = (
            f"{progress_bar(fraction)} {completed}/{discovered} ({fraction * 100:4.1f}%) | "
            f"dl {int(s['downloaded_files'])} files {human_bytes(s['downloaded_bytes'])} "
            f"@ {human_bytes(cur_rate)}/s (avg {human_bytes(avg_rate)}/s) | "
            f"skip {int(s['skipped'])} err {int(s['errors'])} | act {int(s['active'])} | "
            f"{format_duration(elapsed)} elapsed, ETA {format_duration(eta)}"
        )

        if self.is_tty and not final:
            sys.stdout.write("\r" + line)
            sys.stdout.flush()
        else:
            if self.is_tty:
                sys.stdout.write("\n")
                sys.stdout.flush()
            log.info(line)


def has_free_space(directory: str, needed: int) -> bool:
    """True if directory's filesystem can hold `needed` bytes plus the safety margin."""
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        return True  # can't tell; don't block on it
    return free >= needed + RUN.min_free_bytes


def acquire_lock(lock_path: str, force: bool = False) -> bool:
    """Create an exclusive PID lock file. Returns False if a live instance holds it.
    A stale lock (owning PID no longer running) is reclaimed automatically."""
    if os.path.exists(lock_path):
        try:
            existing_pid = int(open(lock_path, encoding="utf-8").read().strip() or "0")
        except (OSError, ValueError):
            existing_pid = 0
        if existing_pid and _pid_alive(existing_pid) and not force:
            return False
        _safe_remove(lock_path)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def release_lock(lock_path: str) -> None:
    try:
        if os.path.exists(lock_path) and open(lock_path, encoding="utf-8").read().strip() == str(os.getpid()):
            _safe_remove(lock_path)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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


def download_atomic(url: str, local_path: str, remote_size: int | None) -> None:
    """Download to a .part sibling and rename into place only after the size is
    verified, so an interrupted or truncated transfer never leaves a file that
    looks complete. Resumes an existing .part when the server supports it, and
    retries with backoff on failure or size mismatch."""
    part = local_path + ".part"
    last_err: str | None = None
    for attempt in range(1, RUN.retries + 1):
        resume = bool(remote_size and os.path.exists(part) and 0 < os.path.getsize(part) < remote_size)
        try:
            curl_download(url, part, resume=resume, timeout=RUN.download_timeout)
        except CurlError as exc:
            last_err = str(exc)
            # An overshoot means the .part is unusable for resume; start clean next time.
            if os.path.exists(part) and remote_size and os.path.getsize(part) > remote_size:
                _safe_remove(part)
            time.sleep(min(30, 3 * attempt))
            continue
        got = os.path.getsize(part) if os.path.exists(part) else 0
        if remote_size is not None and got != remote_size:
            last_err = f"size mismatch (got {got}, expected {remote_size})"
            if got > remote_size:
                _safe_remove(part)
            time.sleep(min(30, 3 * attempt))
            continue
        os.replace(part, local_path)
        return
    raise CurlError(f"download failed after {RUN.retries} attempts for {url}: {last_err}")


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


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
        modified = 0.0
        for cell in tr.find_all("td"):
            raw_date = cell.get("data-sort-value") or cell.get_text(" ", strip=True)
            try:
                modified = float(raw_date)
                if modified > 1000000000:
                    break
                modified = 0.0
            except ValueError:
                pass
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M"):
                try:
                    modified = datetime.strptime(raw_date, fmt).timestamp()
                    break
                except ValueError:
                    continue
            if modified:
                break
        entries.append({
            "href": href,
            "name": unquote(href.rstrip("/")),
            "is_dir": href.endswith("/"),
            "modified": modified,
        })
    entries.sort(
        key=lambda entry: (entry["modified"], not entry["is_dir"], entry["name"].lower()),
        reverse=RUN.content_order == "newest",
    )
    return entries


def run_sync(roots: list[tuple[str, str]], dest_root: str, manifest: "Manifest", crawl_workers: int,
             download_workers: int, verify_all: bool, dry_run: bool, stop_requested: threading.Event,
             initial_files: list[RemoteFile] | None = None,
             max_pending_downloads: int | None = None,
             stats: "ProgressStats | None" = None,
             skip_paths: list[str] | None = None) -> dict[str, int]:
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
    if initial_files and stats:
        stats.add_discovered(len(initial_files))
    discovered += len(initial_files or [])
    pending_downloads = 0
    download_limit = max_pending_downloads or max(download_workers * 4, download_workers)
    skip_filters = [f.lower() for f in (skip_paths or [])]

    def skipped(rel_path: str) -> bool:
        lowered = rel_path.lower()
        return bool(skip_filters and any(f in lowered for f in skip_filters))

    with ThreadPoolExecutor(max_workers=crawl_workers) as crawl_pool, \
            ThreadPoolExecutor(max_workers=download_workers) as dl_pool:
        pending: dict = {}
        for url, rel in roots:
            if skipped(rel):
                log.info("Skipping configured path: %s", rel)
                continue
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
                        if skipped(child_rel):
                            log.info("Skipping configured path: %s", child_rel)
                            continue
                        if entry["is_dir"]:
                            pending[crawl_pool.submit(list_directory, child_url)] = ("list", (child_url, child_rel))
                        else:
                            item = RemoteFile(url=child_url, rel_path=child_rel)
                            discovered += 1
                            if stats:
                                stats.add_discovered()
                            ready_files.append(item)
                else:
                    item = payload
                    pending_downloads -= 1
                    try:
                        result, nbytes = fut.result()
                    except Exception as exc:  # noqa: BLE001 - log and continue
                        log.error("Unexpected error for %s: %s", item.rel_path, exc)
                        result, nbytes = "error", 0
                    counts[result] = counts.get(result, 0) + 1
                    completed += 1
                    if stats:
                        stats.download_finished(result, nbytes)
                    if result == "error-diskfull":
                        log.error("Halting: destination filesystem is full.")
                        stop_requested.set()
                    if completed % 25 == 0:
                        log.info("Progress: %d files completed (%d discovered so far)", completed, discovered)
                    if completed % RUN.manifest_save_every == 0 and not dry_run:
                        manifest.save()

            while ready_files and pending_downloads < download_limit:
                item = ready_files.popleft()
                pending[dl_pool.submit(sync_file, item, dest_root, manifest, verify_all, dry_run)] = \
                    ("sync", item)
                pending_downloads += 1
                if stats:
                    stats.download_started()

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
              verify_all: bool, dry_run: bool) -> tuple[str, int]:
    local_path = os.path.join(dest_root, item.rel_path)
    rel = item.rel_path

    if is_unpacked_archive_duplicate(local_path):
        log.info("Skipping %s (already have the unpacked folder)", rel)
        return "skip-duplicate-archive", 0

    try:
        remote_size = curl_head_size(item.url)
    except CurlError as exc:
        log.error("HEAD failed for %s: %s", item.url, exc)
        return "error", 0

    exists = os.path.exists(local_path)
    local_size = os.path.getsize(local_path) if exists else 0

    if exists and remote_size is not None and local_size == remote_size:
        entry = manifest.get(rel)
        if entry and entry.get("size") == remote_size and not verify_all:
            return "skip-known-good", 0
        digest = sha256_file(local_path)
        if entry and entry.get("sha256") == digest:
            manifest.set(rel, {"size": remote_size, "sha256": digest, "url": item.url,
                                "verified": time.time()})
            return "skip-verified", 0
        if entry and entry.get("sha256") != digest:
            log.warning("Corruption detected for %s (hash mismatch), re-downloading", rel)
        else:
            manifest.set(rel, {"size": remote_size, "sha256": digest, "url": item.url,
                                "verified": time.time()})
            return "baseline-recorded", 0

    part_path = local_path + ".part"
    part_size = os.path.getsize(part_path) if os.path.exists(part_path) else 0

    if dry_run:
        resuming = remote_size and (0 < local_size < remote_size or 0 < part_size < remote_size)
        action = "would-resume" if resuming else "would-download"
        log.info("[dry-run] %s -> %s", action, rel)
        return action, 0

    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    # Only the bytes still needed matter for the space check (resuming a .part).
    needed = (remote_size - part_size) if (remote_size and part_size < remote_size) else (remote_size or 0)
    if remote_size and not has_free_space(os.path.dirname(local_path) or ".", needed):
        log.error("Insufficient free space for %s (needs ~%d bytes)", rel, needed)
        return "error-diskfull", 0

    try:
        download_atomic(item.url, local_path, remote_size)
    except CurlError as exc:
        log.error("Download failed for %s: %s", item.url, exc)
        return "error", 0

    final_size = os.path.getsize(local_path)
    if remote_size is not None and final_size != remote_size:
        log.error("Size mismatch after download for %s (got %d, expected %d)", rel, final_size, remote_size)
        return "error", 0

    digest = sha256_file(local_path)
    manifest.set(rel, {"size": final_size, "sha256": digest, "url": item.url, "verified": time.time()})
    log.info("Downloaded %s (%d bytes)", rel, final_size)
    return "downloaded", final_size


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


def run_defcon_torrent_step(dest_root: str, only: list[str] | None, args: argparse.Namespace,
                            ready_event: threading.Event | None = None,
                            skip: list[str] | None = None,
                            stalled_callback: Callable[[str], None] | None = None,
                            discovery_event: threading.Event | None = None) -> int:
    """Run the DEF CON torrent fetcher in-process so the single-entry workflow remains simple."""
    try:
        from fetch_defcon_torrents import TorrentSettings, fetch_all
    except Exception as exc:  # pragma: no cover - depends on optional libtorrent install
        log.error("Could not import the torrent helper: %s", exc)
        return 1

    torrent_dest = os.path.join(dest_root, "cons", "DEF CON")
    torrent_roots = [
        ("https://media.defcon.org/DEF%20CON%20Torrents/", torrent_dest),
        ("https://infocon.org/", dest_root),
    ]
    defcon_only = [f.strip() for f in args.torrent_defcon_only.split(",") if f.strip()]
    settings = TorrentSettings(
        max_active=args.torrent_max_active,
        connections=args.torrent_connections,
        listen_interface="0.0.0.0:6881",
        poll_seconds=args.torrent_poll_seconds,
        seed_time=args.torrent_seed_time,
        enable_dht=True,
        enable_pex=True,
        enable_lsd=True,
        request_timeout=120,
        retries=3,
        retry_delay=3,
        stalled_minutes=args.torrent_stalled_minutes,
        discovery_workers=args.torrent_discovery_workers,
        max_defcon_active=max(0, args.torrent_max_defcon_active),
        torrent_order=args.torrent_order,
    )
    log.info("Running DEF CON torrent phase into %s ...", torrent_dest)
    return fetch_all(dest=torrent_dest,
                     torrents_dir=os.path.join(os.path.expanduser("~"), ".cache", "infocon-scraper", "torrents"),
                     only=only, settings=settings, ready_event=ready_event, skip=skip,
                     stalled_callback=stalled_callback, torrent_roots=torrent_roots,
                     include_mirrors=args.torrent_include_mirrors,
                     include_rainbow_tables=args.torrent_include_rainbow_tables,
                     defcon_only=defcon_only,
                     discovery_event=discovery_event,
                     index_path=args.torrent_index,
                     index_ttl_hours=max(0, args.torrent_index_ttl_hours))


def build_infocon_roots(root_url: str, only_cons: list[str] | None,
                        only_top: list[str] | None,
                        only_mirrors: list[str] | None,
                        skip_cons: list[str] | None = None) -> list[tuple[str, str]]:
    """Build (url, rel_prefix) roots for infocon.org, ordered DEF CON, then BSides, then the rest."""
    cons_names = discover_cons_folders(root_url)
    if only_cons:
        filters = [f.lower() for f in only_cons]
        cons_names = [n for n in cons_names if any(f in n.lower() for f in filters)]
    if skip_cons:
        filters = [f.lower() for f in skip_cons]
        cons_names = [n for n in cons_names if not any(f in n.lower() for f in filters)]
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


def defcon_torrent_covered_names(media_root: str) -> set[str]:
    """Lowercased base names of DEF CON items that already have a torrent on
    media.defcon.org/DEF CON Torrents/ (e.g. 'def con 30', 'def con conference
    cd dvd collection'). Used to keep the HTTP crawl from re-fetching content
    that the torrent helper already covers."""
    try:
        entries = list_directory(urljoin(media_root, quote("DEF CON Torrents") + "/"))
    except CurlError:
        return set()
    bases: set[str] = set()
    for e in entries:
        if e["is_dir"] or not e["name"].endswith(".torrent"):
            continue
        m = re.match(r"^(.*) v\d+\.torrent$", e["name"])
        if m:
            bases.add(m.group(1).strip().lower())
    return bases


def _is_torrent_covered(folder: str, torrent_bases: set[str]) -> bool:
    f = folder.strip().lower()
    # Exact match, or the torrent base is a longer variant (e.g. folder
    # "DEF CON Conference CD DVD" vs torrent "... CD DVD Collection").
    return any(tb == f or tb.startswith(f + " ") for tb in torrent_bases)


def build_defcon_media_roots(root_url: str, skip_names: set[str] | None = None,
                             skip_torrented: bool = True,
                             torrent_skip_names: list[str] | None = None) -> list[tuple[str, str]]:
    """Expand media.defcon.org's root into per-folder roots, targeting the same
    cons/DEF CON/ location infocon.org uses so both sources land in one place.
    Per-file skip/verify logic in sync_file() already avoids re-downloading
    years that are complete locally - a folder-existence pre-filter would
    wrongly skip years that are only partially downloaded so far. When
    skip_torrented is set, folders that already have a torrent are skipped so
    HTTP only fetches the torrentless remainder (e.g. DEF CON 34)."""
    skip = {n.lower() for n in (skip_names or set())}
    torrent_bases = defcon_torrent_covered_names(root_url) if skip_torrented else set()
    if torrent_skip_names:
        skip_torrents = [name.lower() for name in torrent_skip_names]
        torrent_bases = {
            base for base in torrent_bases
            if not any(name in base for name in skip_torrents)
        }
    names = [
        n for n in discover_top_level_folders(root_url)
        if n.lower() not in skip and not _is_torrent_covered(n, torrent_bases)
    ]
    names.sort(key=lambda n: (conf_priority_rank(n), n.lower()))
    return [(urljoin(root_url, f"{quote(n)}/"), f"cons/DEF CON/{n}") for n in names]


def build_defcon_media_fallback_root(root_url: str, name: str) -> tuple[str, str]:
    """Build the single HTTP root used when a torrent is proven stalled."""
    return urljoin(root_url, f"{quote(name)}/"), f"cons/DEF CON/{name}"


def build_roots(sources: list[str], infocon_root: str, defcon_media_root: str, defcon_media_skip: set[str] | None,
                 only_cons: list[str] | None, only_top: list[str] | None,
                only_mirrors: list[str] | None, skip_cons: list[str] | None = None,
                skip_torrented: bool = True,
                torrent_skip_names: list[str] | None = None) -> list[tuple[str, str]]:
    roots: list[tuple[str, str]] = []
    # media.defcon.org goes first: it's the highest-priority content (DEF CON)
    # and all its roots are submitted for crawling up front, so it shouldn't
    # sit behind ~240 infocon.org conference folders in the submission queue.
    if "defcon-media" in sources:
        roots += build_defcon_media_roots(
            defcon_media_root, defcon_media_skip, skip_torrented, torrent_skip_names
        )
    if "infocon" in sources:
        roots += build_infocon_roots(infocon_root, only_cons, only_top, only_mirrors, skip_cons)
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
                            "'mirrors' (only infocon.org/mirrors), and/or 'defcon-media' (media.defcon.org). "
                            "Default: infocon,defcon-media. DEF CON folders that already have a torrent are "
                            "auto-skipped (grab those with fetch_defcon_torrents.py); HTTP crawls everything "
                            "else, including the torrentless remainder such as DEF CON 34.")
    parser.add_argument("--only-cons", default=None,
                         help="Comma-separated substrings to restrict which infocon.org cons/ conferences are "
                              "synced (default: all conferences)")
    parser.add_argument("--only-top", default=None,
                         help="Comma-separated substrings to restrict which infocon.org top-level sections "
                            "besides cons/ are synced, e.g. 'documentaries,podcasts,rainbow tables' (default: archive sections; rainbow tables require explicit opt-in)")
    parser.add_argument("--only-mirrors", default=None,
                         help="Comma-separated mirror name filters, e.g. 'cryptome,textfiles'; downloads only "
                              "matching collections under infocon.org/mirrors/")
    parser.add_argument("--defcon-media-skip", default=None,
                         help="Comma-separated media.defcon.org folder names to skip entirely, e.g. "
                              "'DEF CON 30,DEF CON 31' when fetching those years via BitTorrent instead")
    parser.add_argument("--skip-recent", default=None,
                        help="Comma-separated substrings to exclude from torrent ownership while leaving matching paths available to HTTP")
    parser.add_argument("--no-skip-torrented", action="store_true",
                         help="When 'defcon-media' is a source, do NOT auto-skip folders that already have a "
                              "torrent (by default such folders are skipped so HTTP only fills gaps like DEF CON 34)")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent download workers")
    parser.add_argument("--max-pending-downloads", type=int, default=None,
                         help="Maximum queued/in-flight file downloads (default: workers * 4)")
    parser.add_argument("--crawl-workers", type=int, default=16, help="Concurrent directory-listing workers")
    parser.add_argument("--status-interval", type=float, default=10.0,
                         help="Seconds between progress/speed status lines (default: 10; 0 disables)")
    parser.add_argument("--content-order", choices=("newest", "oldest"), default="newest",
                         help="Directory/file discovery order (default: newest)")
    parser.add_argument("--retries", type=int, default=4,
                         help="Per-file download attempts before giving up (default: 4)")
    parser.add_argument("--download-timeout", type=int, default=3600,
                         help="Max seconds for a single file download attempt (default: 3600)")
    parser.add_argument("--min-free-gib", type=int, default=1,
                         help="Refuse a download that would leave less than this many GiB free (default: 1)")
    parser.add_argument("--force", action="store_true",
                         help="Override the single-instance lock and run anyway")
    parser.add_argument("--verify-all", action="store_true",
                         help="Re-hash every existing file in scope, not just new ones")
    parser.add_argument("--dry-run", action="store_true", help="List actions without downloading")
    parser.add_argument("--with-torrents", action="store_true",
                        help="Crawl non-DEF CON content during torrent checking, then crawl the torrentless "
                            "DEF CON remainder while torrents continue downloading.")
    parser.add_argument("--torrent-max-active", type=int, default=4,
                         help="If --with-torrents is set, maximum simultaneous torrents to fetch, newest first (default: 4)")
    parser.add_argument("--torrent-max-defcon-active", type=int, default=1,
                         help="If --with-torrents is set, maximum simultaneous DEF CON torrents (default: 1)")
    parser.add_argument("--torrent-order", choices=("newest", "oldest"), default="newest",
                         help="If --with-torrents is set, torrent priority order (default: newest)")
    parser.add_argument("--torrent-connections", type=int, default=800,
                         help="If --with-torrents is set, libtorrent connection cap (default: 800)")
    parser.add_argument("--torrent-poll-seconds", type=int, default=10,
                         help="If --with-torrents is set, progress refresh interval in seconds (default: 10)")
    parser.add_argument("--torrent-seed-time", type=int, default=0,
                         help="If --with-torrents is set, minutes to seed after completion (default: 0)")
    parser.add_argument("--torrent-stalled-minutes", type=int, default=30,
                         help="If --with-torrents is set, hand zero-peer/zero-rate torrents to HTTP after this many minutes (default: 30)")
    parser.add_argument("--torrent-defcon-only", default="30,31,32,33,34",
                         help="DEF CON numbers fetched by combined mode; default: 30,31,32,33,34")
    parser.add_argument("--torrent-include-mirrors", action="store_true",
                         help="Recursively include infocon.org/mirrors torrent files; disabled by default")
    parser.add_argument("--torrent-include-rainbow-tables", action="store_true",
                         help="Recursively include infocon.org/rainbow tables torrents; disabled by default")
    parser.add_argument("--torrent-discovery-workers", type=int, default=8,
                         help="Concurrent recursive torrent listing workers (default: 8)")
    parser.add_argument("--torrent-index", default=None,
                         help="Persistent online torrent inventory index path (default: ~/.cache/infocon-scraper/torrent-index.json)")
    parser.add_argument("--torrent-index-ttl-hours", type=int, default=168,
                         help="Hours before online torrent inventory is rescanned (default: 168 / 7 days)")
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

    RUN.retries = max(1, args.retries)
    RUN.download_timeout = max(1, args.download_timeout)
    RUN.min_free_bytes = max(0, args.min_free_gib) * (1 << 30)
    RUN.content_order = args.content_order

    lock_path = shared_drive_lock_path(args.dest)
    if not acquire_lock(lock_path, force=args.force):
        log.error("Another sync appears to be running for %s (lock: %s). Use --force to override.",
                  args.dest, lock_path)
        return 2

    only_cons = [f.strip() for f in args.only_cons.split(",") if f.strip()] if args.only_cons else None
    only_top = [f.strip() for f in args.only_top.split(",") if f.strip()] if args.only_top else None
    only_mirrors = [f.strip() for f in args.only_mirrors.split(",") if f.strip()] if args.only_mirrors else None
    skip_recent = [f.strip() for f in args.skip_recent.split(",") if f.strip()] if args.skip_recent else None
    defcon_media_skip = {f.strip() for f in args.defcon_media_skip.split(",") if f.strip()} \
        if args.defcon_media_skip else None
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    log.info("Discovering content from sources: %s ...", ", ".join(sources))
    roots = build_roots(sources, args.base_url, args.defcon_media_url, defcon_media_skip,
                        only_cons, only_top, only_mirrors,
                        skip_torrented=not args.no_skip_torrented,
                        torrent_skip_names=skip_recent)
    log.info("Target sections (priority order): %s", ", ".join(rel for _, rel in roots))
    def is_defcon_root(root: tuple[str, str]) -> bool:
        return root[1].lower().startswith("cons/def con")

    defcon_roots = [root for root in roots if is_defcon_root(root)]
    non_defcon_roots = [root for root in roots if not is_defcon_root(root)]
    torrent_thread: threading.Thread | None = None
    torrent_result = [0]
    manifest = Manifest(manifest_path)

    stop_requested = threading.Event()

    def handle_stop(signum, frame):
        log.warning("Signal %s received, finishing in-flight downloads and saving manifest...", signum)
        stop_requested.set()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    initial_files = discover_mirror_files(args.base_url, only_mirrors) \
        if "infocon" in sources or "mirrors" in sources else []

    stats = ProgressStats()
    reporter = StatusReporter(stats, args.status_interval) if args.status_interval > 0 else None
    if reporter:
        reporter.start()

    try:
        counts: dict[str, int] = {}
        fallback_threads: list[threading.Thread] = []
        fallback_counts: dict[str, int] = {}
        fallback_counts_lock = threading.Lock()
        if args.with_torrents:
            torrent_only = only_cons or None
            torrent_ready = threading.Event()
            torrent_discovery = threading.Event()

            def run_stalled_http_fallback(spec) -> None:
                fallback_url = urljoin(spec.url, "./")
                relative_root = os.path.relpath(spec.save_path, args.dest)
                root = (fallback_url, relative_root)
                log.warning("Torrent %s is stalled; starting HTTP fallback for %s", spec.name, root[1])
                result = run_sync(
                    [root], args.dest, manifest, args.crawl_workers, args.workers,
                    args.verify_all, args.dry_run, stop_requested, [],
                    args.max_pending_downloads, stats
                )
                with fallback_counts_lock:
                    for key, value in result.items():
                        fallback_counts[key] = fallback_counts.get(key, 0) + value

            def start_stalled_http_fallback(spec) -> None:
                thread = threading.Thread(
                    target=run_stalled_http_fallback, args=(spec,),
                    name=f"http-fallback-{spec.name}", daemon=False
                )
                fallback_threads.append(thread)
                thread.start()

            def run_torrent_phase() -> None:
                try:
                    torrent_result[0] = run_defcon_torrent_step(
                        args.dest, torrent_only, args, ready_event=torrent_ready, skip=skip_recent,
                        stalled_callback=start_stalled_http_fallback,
                        discovery_event=torrent_discovery
                    )
                except Exception:
                    log.exception("DEF CON torrent phase failed unexpectedly.")
                    torrent_result[0] = 1
                finally:
                    torrent_discovery.set()
                    torrent_ready.set()

            torrent_thread = threading.Thread(
                target=run_torrent_phase, name="defcon-torrents", daemon=False
            )
            torrent_thread.start()
            log.info("Waiting for the complete online torrent inventory before starting HTTP ...")
            torrent_discovery.wait()
            if non_defcon_roots:
                log.info("Online torrent inventory complete; crawling non-DEF CON content ...")
                for key, value in run_sync(
                    non_defcon_roots, args.dest, manifest, args.crawl_workers, args.workers,
                    args.verify_all, args.dry_run, stop_requested, initial_files,
                    args.max_pending_downloads, stats
                ).items():
                    counts[key] = counts.get(key, 0) + value
            log.info("Waiting for initial DEF CON torrent file checking before crawling DEF CON HTTP remainder ...")
            torrent_ready.wait()
            if torrent_result[0] != 0:
                log.error("DEF CON torrent phase failed before its HTTP remainder could start.")
            elif defcon_roots:
                log.info("Initial DEF CON torrent checking finished; crawling torrentless DEF CON HTTP remainder ...")
                for key, value in run_sync(
                    defcon_roots, args.dest, manifest, args.crawl_workers, args.workers,
                    args.verify_all, args.dry_run, stop_requested, [],
                    args.max_pending_downloads, stats
                ).items():
                    counts[key] = counts.get(key, 0) + value
        else:
            counts = run_sync(roots, args.dest, manifest, args.crawl_workers, args.workers,
                              args.verify_all, args.dry_run, stop_requested, initial_files,
                              args.max_pending_downloads, stats)
    finally:
        if torrent_thread:
            torrent_thread.join()
        for fallback_thread in fallback_threads:
            fallback_thread.join()
        if reporter:
            reporter.stop()
            reporter.join(timeout=args.status_interval + 2)
        manifest.save()
        release_lock(lock_path)

    for key, value in fallback_counts.items():
        counts[key] = counts.get(key, 0) + value
    final = stats.snapshot()
    elapsed = time.time() - stats.start
    avg_rate = final["downloaded_bytes"] / elapsed if elapsed > 0 else 0.0
    log.info("Done in %s. Downloaded %d files (%s) at %s/s avg; %d skipped, %d errors.",
             format_duration(elapsed), int(final["downloaded_files"]),
             human_bytes(final["downloaded_bytes"]), human_bytes(avg_rate),
             int(final["skipped"]), int(final["errors"]))
    log.info("Summary: %s", json.dumps(counts, indent=2))
    return 1 if counts.get("error") or torrent_result[0] else 0


if __name__ == "__main__":
    sys.exit(main())
