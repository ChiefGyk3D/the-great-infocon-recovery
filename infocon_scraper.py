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
import heapq
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from collections.abc import Callable
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

import ddv_profiles

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
# Opt-in only: mirrors/ (vx underground malware samples, textfiles.com, etc.)
# and rainbow tables/ are each large enough to fill a drive on their own, so
# they are reachable through --only-top but never part of the default set.
OPT_IN_TOP_LEVEL_SECTIONS = ["mirrors", "rainbow tables"]
ALL_TOP_LEVEL_SECTIONS = TOP_LEVEL_SECTIONS + OPT_IN_TOP_LEVEL_SECTIONS
# Priority conference names under cons/: DEF CON first, then any conference
# whose name contains "bsides" (case-insensitive), then the rest.
USER_AGENT = "InfoConDriveSync/1.0 (personal archive sync tool)"
CHUNK_SIZE = 1 << 20  # 1 MiB

log = logging.getLogger("infocon_scraper")


@dataclass
class RemoteFile:
    url: str
    rel_path: str
    # Published metadata from the directory listing. `modified` drives the
    # size+mtime fast path that avoids a HEAD per file on a refresh; `size` is
    # a rounded scheduling hint, never used for verification.
    modified: float = 0.0
    size: int | None = None
    size_slack: int = 0


class CurlError(RuntimeError):
    def __init__(self, message: str, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode


# curl exit 33: the server ignored our Range request, so a partial file cannot
# be continued and must be fetched again from the start.
CURL_RANGE_UNSUPPORTED = 33


@dataclass
class RunConfig:
    retries: int = 4
    # 0 disables the hard per-attempt wall-clock cap. A fixed cap cannot work
    # for this archive: a 128 GB word list at a few MB/s needs many hours, and
    # curl restarts an --max-time abort from byte zero, so a capped attempt can
    # never converge. Stalls are detected by throughput instead (see below).
    download_timeout: int = 0
    # Abort a transfer that averages less than min_speed_bytes/s for
    # stall_seconds - a genuinely dead connection, not merely a slow one.
    min_speed_bytes: int = 1024
    stall_seconds: int = 300
    # 0 keeps the per-host defaults in HOST_CONCURRENCY_LIMITS.
    metadata_connections: int = 0
    transfer_connections: int = 0
    hash_workers: int = 2
    large_file_bytes: int = 1 << 30
    max_large_downloads: int = 2
    listing_retries: int = 3
    listing_retry_delay: float = 5.0
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


# Bytes already on disk in .part files that have not completed yet. Without
# this the reported rate only moves when a file finishes, so a run pulling
# several multi-gigabyte archives displays 0 B/s and a nonsense ETA for hours.
_inflight_parts: dict[str, int] = {}
_inflight_lock = threading.Lock()


def register_part(part_path: str, baseline: int) -> None:
    with _inflight_lock:
        _inflight_parts[part_path] = baseline


def unregister_part(part_path: str) -> None:
    with _inflight_lock:
        _inflight_parts.pop(part_path, None)


def inflight_bytes() -> int:
    """Bytes fetched so far by transfers still in progress."""
    with _inflight_lock:
        staged = list(_inflight_parts.items())
    total = 0
    for path, baseline in staged:
        try:
            total += max(0, os.path.getsize(path) - baseline)
        except OSError:
            continue
    return total


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
        # Completed bytes plus whatever in-flight transfers have staged, so the
        # rate reflects work actually happening rather than only completions.
        moved_bytes = s["downloaded_bytes"] + inflight_bytes()
        cur_rate = (moved_bytes - self._last_bytes) / window if window > 0 else 0.0
        avg_rate = moved_bytes / elapsed if elapsed > 0 else 0.0
        self._last_time = now
        self._last_bytes = moved_bytes

        discovered = s["discovered"]
        completed = s["completed"]
        fraction = (completed / discovered) if discovered else 0.0
        comp_rate = completed / elapsed if elapsed > 0 else 0.0
        remaining = max(discovered - completed, 0)
        eta = remaining / comp_rate if comp_rate > 0 else 0.0

        line = (
            f"{progress_bar(fraction)} {completed}/{discovered} ({fraction * 100:4.1f}%) | "
            f"dl {int(s['downloaded_files'])} files {human_bytes(moved_bytes)} "
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
            with open(lock_path, encoding="utf-8") as handle:
                existing_pid = int(handle.read().strip() or "0")
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
        with open(lock_path, encoding="utf-8") as handle:
            owner = handle.read().strip()
    except OSError:
        return
    if owner == str(os.getpid()):
        _safe_remove(lock_path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# These hosts throttle or drop connections once too many are open at once, so
# concurrency is capped per host. Metadata and transfers get separate budgets:
# a single semaphore shared by both meant a handful of multi-gigabyte downloads
# held every slot for hours, starving directory listings and HEAD requests on
# the same host and stalling discovery completely.
METADATA = "metadata"
TRANSFER = "transfer"

HOST_CONCURRENCY_LIMITS = {
    "infocon.org": {METADATA: 4, TRANSFER: 4},
    "media.defcon.org": {METADATA: 6, TRANSFER: 6},
}
_host_semaphores: dict[tuple[str, str], threading.Semaphore] = {}
_host_semaphores_lock = threading.Lock()


def host_limit(host: str, kind: str) -> int | None:
    """Effective concurrency for a host, honouring any CLI override."""
    limits = HOST_CONCURRENCY_LIMITS.get(host)
    if not limits:
        return None
    override = RUN.metadata_connections if kind == METADATA else RUN.transfer_connections
    return override if override else limits.get(kind)


def host_semaphore(url: str, kind: str = METADATA) -> threading.Semaphore | None:
    """The connection budget for *url*, or None when the host is uncapped."""
    host = urlparse(url).netloc
    limit = host_limit(host, kind)
    if not limit:
        return None
    key = (host, kind)
    with _host_semaphores_lock:
        sem = _host_semaphores.get(key)
        if sem is None:
            sem = threading.Semaphore(limit)
            _host_semaphores[key] = sem
        return sem


def _reset_host_semaphores() -> None:
    """Test hook: drops cached semaphores so new limits take effect."""
    with _host_semaphores_lock:
        _host_semaphores.clear()


# Neither infocon.org nor media.defcon.org honours a Range request - both
# answer 200 with the whole body - so a partial transfer cannot be continued
# there. Discovered per host at runtime rather than assumed, since the same
# code serves other mirrors.
_no_range_hosts: set[str] = set()
_no_range_lock = threading.Lock()


def host_supports_ranges(url: str) -> bool:
    with _no_range_lock:
        return urlparse(url).netloc not in _no_range_hosts


def mark_host_without_ranges(url: str) -> bool:
    """Record that a host ignores Range. Returns True the first time."""
    host = urlparse(url).netloc
    with _no_range_lock:
        if host in _no_range_hosts:
            return False
        _no_range_hosts.add(host)
        return True


def _reset_range_support() -> None:
    """Test hook."""
    with _no_range_lock:
        _no_range_hosts.clear()


def run_curl(args: list[str], timeout: int, url: str | None = None,
             stall_guard: bool = False, kind: str = METADATA) -> subprocess.CompletedProcess:
    """Run curl under the per-host concurrency budget for *kind*.

    *timeout* is a hard wall-clock cap and is only applied when positive; bulk
    transfers pass stall_guard=True instead so a slow-but-alive download is
    never killed and restarted from the beginning.
    """
    command = ["curl", "-sS", "-A", USER_AGENT, "--retry", "3", "--retry-delay", "3",
               "--retry-all-errors", "--connect-timeout", "30"]
    if stall_guard:
        command += ["--speed-limit", str(RUN.min_speed_bytes), "--speed-time", str(RUN.stall_seconds)]
    if timeout and timeout > 0:
        command += ["--max-time", str(timeout)]
    sem = host_semaphore(url, kind) if url else None
    if sem:
        sem.acquire()
    try:
        return subprocess.run(command + args, capture_output=True, text=False)
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


def curl_download(url: str, local_path: str, resume: bool, timeout: int = 0) -> None:
    args = ["-L", "--fail", "-o", local_path]
    if resume:
        # Also makes curl's own --retry resume rather than truncate and restart.
        args += ["-C", "-"]
    args += [url]
    proc = run_curl(args, timeout, url=url, stall_guard=True, kind=TRANSFER)
    if proc.returncode != 0:
        raise CurlError(
            f"curl download failed ({proc.returncode}) for {url}: "
            f"{proc.stderr.decode(errors='replace').strip()}",
            returncode=proc.returncode,
        )


def download_atomic(url: str, local_path: str, expected_size: int | None,
                    slack: int = 0) -> None:
    """Download to a .part sibling and rename into place only once it looks
    complete, so an interrupted transfer never leaves a file that looks whole.

    curl's exit status is the primary integrity signal: neither archive host
    sends Content-Length, so a truncated response surfaces as a curl transfer
    error rather than a short body. *expected_size* is the directory listing's
    published size and acts as a second check, compared within *slack* because
    that value is published rounded.
    """
    part = local_path + ".part"
    register_part(part, os.path.getsize(part) if os.path.exists(part) else 0)
    try:
        _download_attempts(url, local_path, expected_size, part, slack)
    finally:
        unregister_part(part)


def _download_attempts(url: str, local_path: str, expected_size: int | None, part: str,
                       slack: int = 0) -> None:
    last_err: str | None = None
    for attempt in range(1, RUN.retries + 1):
        part_size = os.path.getsize(part) if os.path.exists(part) else 0
        # Resume whenever staged bytes exist and the host actually honours
        # Range; restarting a multi-gigabyte transfer from zero is never the
        # cheaper option where continuing is possible.
        resume = (part_size > 0 and host_supports_ranges(url)
                  and (expected_size is None or part_size < expected_size))
        try:
            curl_download(url, part, resume=resume, timeout=RUN.download_timeout)
        except CurlError as exc:
            last_err = str(exc)
            if exc.returncode == CURL_RANGE_UNSUPPORTED and resume:
                # The host ignores Range, so the staged bytes are unusable and
                # every future resume against it would fail the same way.
                if mark_host_without_ranges(url):
                    log.warning("%s does not support resuming; transfers there restart from the "
                                "beginning when interrupted.", urlparse(url).netloc)
                _safe_remove(part)
                continue
            # An overshoot means the .part is unusable for resume; start clean next time.
            if os.path.exists(part) and expected_size and os.path.getsize(part) > expected_size + slack:
                _safe_remove(part)
            time.sleep(min(30, 3 * attempt))
            continue
        got = os.path.getsize(part) if os.path.exists(part) else 0
        if expected_size is not None and abs(got - expected_size) > slack:
            last_err = (f"size mismatch (got {got}, expected {expected_size}"
                        f"{f' +/- {slack}' if slack else ''})")
            if got > expected_size + slack:
                _safe_remove(part)
            time.sleep(min(30, 3 * attempt))
            continue
        try:
            os.replace(part, local_path)
        except OSError as exc:
            # Another worker downloading the same path would race on this
            # rename; _claim_path() is what prevents that, so surface a clear
            # message instead of a bare ENOENT if it ever happens again.
            raise CurlError(f"could not stage {part} into place for {url}: {exc}") from exc
        return
    raise CurlError(f"download failed after {RUN.retries} attempts for {url}: {last_err}")


# Directories an in-progress torrent is writing into. HTTP must not touch them:
# libtorrent allocates files sparsely, so a half-downloaded file already reports
# its FINAL size on disk. HTTP compares size against the directory listing to
# decide "is this complete?", so a mostly-empty file passes, gets hashed, and is
# recorded as good forever - and re-downloading it in parallel would corrupt the
# torrent's own writes. Ownership is released when a torrent completes or is
# handed to the HTTP fallback.
_torrent_paths: set[str] = set()
_torrent_paths_lock = threading.Lock()


def claim_torrent_path(directory: str) -> None:
    with _torrent_paths_lock:
        _torrent_paths.add(os.path.normpath(directory))


def release_torrent_path(directory: str) -> None:
    with _torrent_paths_lock:
        _torrent_paths.discard(os.path.normpath(directory))


def is_torrent_owned(local_path: str) -> bool:
    """True while an in-progress torrent owns the file's directory."""
    path = os.path.normpath(local_path)
    with _torrent_paths_lock:
        if not _torrent_paths:
            return False
        owned = tuple(_torrent_paths)
    return any(path == d or path.startswith(d + os.sep) for d in owned)


def _reset_torrent_paths() -> None:
    """Test hook."""
    with _torrent_paths_lock:
        _torrent_paths.clear()


SPARSE_ALLOCATION_RATIO = 0.95


def looks_incomplete(local_path: str, expected_size: int | None) -> bool:
    """True when a file claims the right size but is not actually filled in.

    libtorrent writes sparse files, so an interrupted torrent leaves a
    full-size file containing holes. Size alone cannot tell it apart from a
    finished download; allocated blocks can.
    """
    if not expected_size:
        return False
    try:
        st = os.stat(local_path)
    except OSError:
        return False
    if st.st_size != expected_size and abs(st.st_size - expected_size) > 0:
        return False
    allocated = st.st_blocks * 512
    return allocated < st.st_size * SPARSE_ALLOCATION_RATIO


# Guards against two workers writing the same destination file at once. The
# duplicate crawls that used to make this happen are fixed at the source, but a
# collision here silently corrupts a .part and then fails its rename, so the
# invariant is enforced rather than assumed.
_active_paths: set[str] = set()
_finished_paths: set[str] = set()
_paths_lock = threading.Lock()


def _claim_path(local_path: str) -> str | None:
    """Reserve *local_path* for this worker.

    Returns the claim token, or None when another worker holds the path or has
    already synced it during this run.
    """
    key = os.path.normpath(local_path)
    with _paths_lock:
        if key in _active_paths or key in _finished_paths:
            return None
        _active_paths.add(key)
    return key


def _release_path(key: str, finished: bool) -> None:
    with _paths_lock:
        _active_paths.discard(key)
        if finished:
            _finished_paths.add(key)


def _reset_path_registry() -> None:
    """Test hook: clears the cross-run_sync duplicate-download registry."""
    with _paths_lock:
        _active_paths.clear()
        _finished_paths.clear()


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# Both infocon.org and media.defcon.org render dates as "2025 Dec 25 09:30".
# None of the previously attempted formats matched, so every entry parsed as
# modified=0.0 and --content-order silently degraded to a name sort.
LISTING_DATE_FORMATS = (
    "%Y %b %d %H:%M",
    "%Y %b %d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%d-%b-%Y %H:%M",
    "%Y-%b-%d %H:%M",
)
# fancyindex prints binary units ("648.6 KiB"); a few themes use decimal ones.
LISTING_SIZE_UNITS = {
    "B": 1,
    "KIB": 1 << 10, "MIB": 1 << 20, "GIB": 1 << 30, "TIB": 1 << 40, "PIB": 1 << 50,
    "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40, "P": 1 << 50,
    "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4, "PB": 1000 ** 5,
}


def parse_listing_date(raw: str) -> float:
    """Epoch seconds from a listing's date cell, or 0.0 when unknown."""
    text = (raw or "").strip()
    if not text or text == "-":
        return 0.0
    try:
        numeric = float(text)
    except ValueError:
        pass
    else:
        # data-sort-value carries a raw epoch on themes that provide it.
        return numeric if numeric > 1_000_000_000 else 0.0
    for fmt in LISTING_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def parse_listing_size(raw: str) -> int | None:
    """Byte count from a listing's size cell, or None when unknown.

    Neither archive host sends Content-Length, so this is the only size
    information available before a file is fetched. It is published rounded
    ("648.6 KiB"), so pair it with parse_listing_size_slack() when comparing.
    """
    parsed = _parse_listing_size_parts(raw)
    return None if parsed is None else parsed[0]


def parse_listing_size_slack(raw: str) -> int:
    """How far a real byte count may differ from the listed one.

    The listing shows a fixed number of decimals in whichever unit fits, so the
    uncertainty is one display step - 0.1 KiB is 102 bytes, 0.1 GiB is 107 MB -
    rather than a flat percentage. A plain byte count is exact.
    """
    parsed = _parse_listing_size_parts(raw)
    return 0 if parsed is None else parsed[1]


def _parse_listing_size_parts(raw: str) -> tuple[int, int] | None:
    text = (raw or "").strip()
    if not text or text == "-":
        return None
    match = re.fullmatch(r"([0-9]+(?:\.([0-9]+))?)\s*([A-Za-z]*)", text)
    if not match:
        return None
    multiplier = LISTING_SIZE_UNITS.get(match.group(3).upper() or "B")
    if multiplier is None:
        return None
    value = int(float(match.group(1)) * multiplier)
    decimals = match.group(2)
    if multiplier == 1:
        slack = 0  # a byte count is published exactly
    else:
        step = 10 ** -len(decimals) if decimals else 1
        slack = max(1, int(step * multiplier))
    return value, slack


def safe_child_name(name: str) -> str | None:
    """A listing name usable as one path segment, or None if it would escape.

    Listing names are remote input joined straight onto the destination path, so
    an absolute or traversing name would write outside the drive.
    """
    if not name or name in (os.curdir, os.pardir):
        return None
    if os.path.isabs(name) or "/" in name or "\\" in name:
        return None
    if os.path.basename(name) != name:
        return None
    return name


def _cell_text(cell) -> str:
    return cell.get("data-sort-value") or cell.get_text(" ", strip=True)


def list_directory(url: str) -> list[dict]:
    """Parse one fancyindex directory listing page into entries.

    Each entry carries the published modification time and, when the listing
    provides it, an approximate size used for download scheduling.
    """
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
        name = safe_child_name(unquote(href.rstrip("/")))
        if name is None:
            log.warning("Ignoring listing entry %r under %s: not a usable path segment", href, url)
            continue

        cells = tr.find_all("td")
        date_text = ""
        size_text = ""
        for cell in cells:
            classes = cell.get("class") or []
            if "date" in classes and not date_text:
                date_text = _cell_text(cell)
            elif "size" in classes and not size_text:
                size_text = _cell_text(cell)
        modified = parse_listing_date(date_text)
        if not modified and not date_text:
            # Theme without a class="date" cell: fall back to scanning cells.
            for cell in cells:
                modified = parse_listing_date(_cell_text(cell))
                if modified:
                    break

        entries.append({
            "href": href,
            "name": name,
            "is_dir": href.endswith("/"),
            "modified": modified,
            "size": parse_listing_size(size_text),
            "size_slack": parse_listing_size_slack(size_text),
        })

    newest_first = RUN.content_order == "newest"
    entries.sort(key=lambda entry: (
        -entry["modified"] if newest_first else entry["modified"],
        0 if not entry["is_dir"] else 1,  # start transferring before recursing
        entry["name"].lower(),
    ))
    return entries


def is_large_transfer(item: RemoteFile) -> bool:
    """True when the listing says this file is big enough to hog a slot.

    Unknown sizes count as small: the listing hint is missing for some themes,
    and treating those as large would stall the queue behind them.
    """
    return bool(item.size and item.size >= RUN.large_file_bytes)


def run_sync(roots: list[tuple[str, str]], dest_root: str, manifest: Manifest, crawl_workers: int,
             download_workers: int, verify_all: bool, dry_run: bool, stop_requested: threading.Event,
             initial_files: list[RemoteFile] | None = None,
             max_pending_downloads: int | None = None,
             stats: ProgressStats | None = None,
             skip_paths: list[str] | None = None,
             initial_files_already_counted: bool = False) -> dict[str, int]:
    """Crawl and download at the same time: as soon as a file is discovered it's
    handed to the download pool immediately, instead of waiting for the entire
    site to be crawled first. This is what lets priority roots (DEF CON,
    BSides - submitted first) start downloading right away, without being
    blocked behind huge, slow-to-list trees (e.g. vx underground's hundreds of
    thousands of malware-sample folders) discovered from later roots.

    Very large files are limited to `--max-large-downloads` concurrent
    transfers. Without that cap the queue order alone decides scheduling, and a
    run that happens to reach several multi-gigabyte archives at once gives up
    every download slot to them for hours.
    """
    counts: dict[str, int] = {}
    discovered = 0
    completed = 0
    ready_small: deque[RemoteFile] = deque()
    ready_large: deque[RemoteFile] = deque()

    def enqueue(item: RemoteFile) -> None:
        (ready_large if is_large_transfer(item) else ready_small).append(item)

    for item in initial_files or []:
        enqueue(item)
    if initial_files and stats and not initial_files_already_counted:
        stats.add_discovered(len(initial_files))
    discovered += len(initial_files or [])
    pending_downloads = 0
    active_large = 0
    download_limit = max_pending_downloads or max(download_workers * 4, download_workers)
    large_limit = max(1, RUN.max_large_downloads)
    skip_filters = [f.lower() for f in (skip_paths or [])]

    def skipped(rel_path: str) -> bool:
        lowered = rel_path.lower()
        return bool(skip_filters and any(f in lowered for f in skip_filters))

    def next_ready() -> RemoteFile | None:
        """Pick the next transfer, keeping large files to their own budget."""
        nonlocal active_large
        if ready_large and active_large < large_limit:
            active_large += 1
            return ready_large.popleft()
        if ready_small:
            return ready_small.popleft()
        return None

    # Not a `with` block: exiting one calls shutdown(wait=True), which drains
    # every *queued* task before returning. On a stop signal mid-crawl that
    # means waiting out the entire remaining directory queue - a SIGTERM took
    # minutes to be honoured instead of seconds. Queued work is cancelled
    # explicitly instead; tasks already running still finish.
    crawl_pool = ThreadPoolExecutor(max_workers=crawl_workers, thread_name_prefix="crawl")
    dl_pool = ThreadPoolExecutor(max_workers=download_workers, thread_name_prefix="download")
    try:
        pending: dict = {}
        # Listings that finished while the download queue was full. They are
        # parked here rather than left in `pending`, where an already-completed
        # future made wait(FIRST_COMPLETED) return instantly on every iteration
        # and spin the loop at full CPU for as long as downloads stayed busy.
        deferred_listings: deque = deque()

        def expand_listing(fut, url: str, rel: str) -> None:
            nonlocal discovered
            try:
                entries = fut.result()
            except Exception as exc:  # noqa: BLE001 - a single bad listing must not kill the whole crawl
                log.error("Failed to list %s: %s", url, exc)
                return
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
                    discovered += 1
                    if stats:
                        stats.add_discovered()
                    enqueue(RemoteFile(url=child_url, rel_path=child_rel,
                                       modified=entry["modified"], size=entry.get("size"),
                                       size_slack=entry.get("size_slack", 0)))

        for url, rel in roots:
            if skipped(rel):
                log.info("Skipping configured path: %s", rel)
                continue
            pending[crawl_pool.submit(list_directory, url)] = ("list", (url, rel))

        # Scheduling happens before waiting, so a call with no roots but a
        # pre-built file list still transfers. Driving the loop off `pending`
        # alone meant run_sync([], initial_files=...) - exactly how combined
        # mode hands over the shared inventory - returned instantly having
        # downloaded nothing at all.
        while True:
            if stop_requested.is_set():
                break

            while deferred_listings and pending_downloads < download_limit:
                fut, (url, rel) = deferred_listings.popleft()
                expand_listing(fut, url, rel)

            while pending_downloads < download_limit:
                item = next_ready()
                if item is None:
                    break
                pending[dl_pool.submit(sync_file, item, dest_root, manifest, verify_all, dry_run)] = \
                    ("sync", item)
                pending_downloads += 1
                if stats:
                    stats.download_started()

            if not pending:
                # Nothing is in flight, so nothing can free capacity later.
                queued = len(ready_small) + len(ready_large)
                if queued or deferred_listings:
                    log.error("Stopping with %d discovered files still queued", queued)
                break

            done, _ = wait(list(pending.keys()), return_when=FIRST_COMPLETED)
            for fut in done:
                kind, payload = pending.pop(fut)
                if kind == "list":
                    if pending_downloads >= download_limit:
                        deferred_listings.append((fut, payload))
                        continue
                    expand_listing(fut, *payload)
                    continue
                item = payload
                pending_downloads -= 1
                if is_large_transfer(item):
                    active_large -= 1
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

    finally:
        cancelled = stop_requested.is_set()
        crawl_pool.shutdown(wait=True, cancel_futures=cancelled)
        dl_pool.shutdown(wait=True, cancel_futures=cancelled)
        # Persist whatever this pass recorded, rather than leaving it buffered
        # until the next 200-completion checkpoint that may never arrive.
        if not dry_run:
            manifest.save()

    log.info("Progress: %d files completed (%d discovered total)", completed, discovered)
    return counts


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(block)
    return h.hexdigest()


MANIFEST_FLUSH_EVERY = 500
MANIFEST_COLUMNS = ("size", "sha256", "url", "mtime", "verified")


def manifest_db_path(path: str) -> str:
    """Storage path for the manifest, given whatever the user asked for."""
    if path == ":memory:":
        return path
    return path[:-5] + ".db" if path.endswith(".json") else path


class Manifest:
    """Verification records for every synced file, stored in SQLite.

    This used to be one JSON document held entirely in memory and rewritten in
    full every 200 completions. Across the archive's ~450k files that means
    serialising tens of megabytes thousands of times - and doing it under the
    same lock every worker needs to read through, so the entire pool stalled on
    each save. SQLite in WAL mode writes only what changed and never blocks
    readers behind a full rewrite.

    An existing .infocon_manifest.json is imported once on first use, so an
    established drive keeps every hash it has already recorded.
    """

    def __init__(self, path: str):
        self.path = manifest_db_path(path)
        self.lock = threading.RLock()
        self._pending: dict[str, dict] = {}
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        with self.lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS entries ("
                "rel TEXT PRIMARY KEY, size INTEGER, sha256 TEXT, url TEXT, "
                "mtime REAL, verified REAL)"
            )
            self._conn.commit()
        self._import_legacy(path)

    def _import_legacy(self, requested_path: str) -> None:
        """Load a pre-SQLite JSON manifest exactly once."""
        if self.path == ":memory:":
            return
        legacy = requested_path if requested_path.endswith(".json") else self.path[:-3] + ".json"
        if not os.path.exists(legacy):
            return
        with self.lock:
            existing = self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        if existing:
            return
        try:
            with open(legacy, encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read legacy manifest %s (%s), starting fresh", legacy, exc)
            return
        if not isinstance(data, dict) or not data:
            return
        log.info("Importing %d entries from the legacy JSON manifest %s ...", len(data), legacy)
        for rel, entry in data.items():
            if isinstance(entry, dict):
                self._pending[rel] = entry
        self.save()
        log.info("Manifest migrated to %s; the JSON copy is no longer read or updated.", self.path)

    def get(self, rel_path: str) -> dict | None:
        with self.lock:
            if rel_path in self._pending:
                return dict(self._pending[rel_path])
            row = self._conn.execute(
                f"SELECT {', '.join(MANIFEST_COLUMNS)} FROM entries WHERE rel = ?", (rel_path,)
            ).fetchone()
        if row is None:
            return None
        return {name: value for name, value in zip(MANIFEST_COLUMNS, row, strict=True)
                if value is not None}

    def set(self, rel_path: str, entry: dict) -> None:
        with self.lock:
            self._pending[rel_path] = entry
            overflowing = len(self._pending) >= MANIFEST_FLUSH_EVERY
        if overflowing:
            self.save()

    def save(self) -> None:
        """Flush buffered entries. Cheap and incremental, unlike a full rewrite."""
        with self.lock:
            if not self._pending:
                return
            rows = [
                (rel,) + tuple(entry.get(name) for name in MANIFEST_COLUMNS)
                for rel, entry in self._pending.items()
            ]
            self._pending.clear()
            self._conn.executemany(
                f"INSERT OR REPLACE INTO entries (rel, {', '.join(MANIFEST_COLUMNS)}) "
                f"VALUES (?{', ?' * len(MANIFEST_COLUMNS)})",
                rows,
            )
            self._conn.commit()

    def count(self) -> int:
        self.save()
        with self.lock:
            return self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def close(self) -> None:
        self.save()
        with self.lock:
            self._conn.close()


def _record(manifest: Manifest, rel: str, item: RemoteFile, size: int,
            digest: str | None) -> None:
    """Write a manifest entry, preserving a known hash when none is supplied."""
    entry = {
        "size": size,
        "url": item.url,
        "mtime": item.modified or None,
        "verified": time.time(),
    }
    if digest is None:
        existing = manifest.get(rel) or {}
        digest = existing.get("sha256")
    if digest:
        entry["sha256"] = digest
    manifest.set(rel, entry)


# Hashing a freshly downloaded file means reading it back in full. Doing that
# inside the download worker holds a scarce download slot for minutes on a
# multi-gigabyte file, so it runs on its own small pool instead.
_hash_pool: ThreadPoolExecutor | None = None
_hash_futures: list = []
_hash_lock = threading.Lock()


def schedule_hash(manifest: Manifest, rel: str, local_path: str,
                  item: RemoteFile, size: int) -> None:
    global _hash_pool

    def run() -> None:
        try:
            digest = sha256_file(local_path)
        except OSError as exc:
            log.warning("Could not hash %s: %s", rel, exc)
            return
        _record(manifest, rel, item, size, digest)

    with _hash_lock:
        if RUN.hash_workers <= 0:
            run()
            return
        if _hash_pool is None:
            _hash_pool = ThreadPoolExecutor(max_workers=RUN.hash_workers,
                                            thread_name_prefix="hash")
        _hash_futures.append(_hash_pool.submit(run))
        # Keep the tracking list from growing across a long run.
        if len(_hash_futures) > 512:
            _hash_futures[:] = [f for f in _hash_futures if not f.done()]


def drain_hashes() -> None:
    """Block until every queued hash has been recorded in the manifest."""
    global _hash_pool
    with _hash_lock:
        pool, _hash_pool = _hash_pool, None
        pending = list(_hash_futures)
        _hash_futures.clear()
    if not pool:
        return
    outstanding = sum(1 for f in pending if not f.done())
    if outstanding:
        log.info("Waiting for %d outstanding file hash(es) ...", outstanding)
    pool.shutdown(wait=True)


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
    """Sync one remote file, guaranteeing exclusive ownership of its local path.

    The concurrent HTTP phases (main sync plus any stalled-torrent fallback) can
    legitimately discover the same file, and two workers sharing one `.part`
    corrupt it and then race on the rename into place.
    """
    local_path = os.path.join(dest_root, item.rel_path)
    claim = _claim_path(local_path)
    if claim is None:
        return "skip-duplicate-path", 0
    finished = False
    try:
        result, nbytes = _sync_one_file(item, local_path, manifest, verify_all, dry_run)
        finished = not result.startswith("error")
        return result, nbytes
    finally:
        _release_path(claim, finished)


MTIME_TOLERANCE_SECONDS = 2.0


def _listing_matches_manifest(item: RemoteFile, entry: dict | None, local_size: int) -> bool:
    """True when the listing agrees with what we already recorded for this file.

    Lets a refresh skip a verified file without a HEAD request. Matching on the
    published modification time rather than the listed size is deliberate: the
    listed size is rounded, the timestamp is exact. Entries written before
    mtimes were recorded simply miss the fast path once, then gain one.
    """
    if not entry or not item.modified:
        return False
    recorded_mtime = entry.get("mtime")
    if not recorded_mtime:
        return False
    if abs(float(recorded_mtime) - item.modified) > MTIME_TOLERANCE_SECONDS:
        return False
    return entry.get("size") == local_size


def _sync_one_file(item: RemoteFile, local_path: str, manifest: Manifest,
                   verify_all: bool, dry_run: bool) -> tuple[str, int]:
    rel = item.rel_path

    if is_unpacked_archive_duplicate(local_path):
        log.info("Skipping %s (already have the unpacked folder)", rel)
        return "skip-duplicate-archive", 0

    if is_torrent_owned(local_path):
        # A torrent is fetching this right now, with piece-level verification.
        # Fetching it again over HTTP would duplicate the transfer and race the
        # torrent's own writes on the same file.
        return "skip-torrent-owned", 0

    exists = os.path.exists(local_path)
    local_size = os.path.getsize(local_path) if exists else 0
    entry = manifest.get(rel)

    # Consult what we already know before touching the network. This used to be
    # a HEAD per file regardless, which on a refresh of a complete drive is one
    # request per file - hundreds of thousands of them - to learn nothing.
    if exists and not verify_all and _listing_matches_manifest(item, entry, local_size):
        return "skip-known-good", 0

    # The directory listing is the only size the archive hosts publish: neither
    # infocon.org nor media.defcon.org returns Content-Length, so a HEAD per
    # file bought nothing but a round trip. It is kept only as a fallback for
    # listings that omit a size, and for other hosts that do answer.
    expected_size, slack = item.size, item.size_slack
    if expected_size is None:
        try:
            expected_size = curl_head_size(item.url)
            slack = 0
        except CurlError as exc:
            log.error("HEAD failed for %s: %s", item.url, exc)
            return "error", 0

    if exists and looks_incomplete(local_path, local_size):
        # Full-size but unfilled: an abandoned torrent's sparse file. Its bytes
        # are unusable and the host cannot resume, so start clean.
        log.warning("Discarding partially written %s (only %.0f%% allocated)", rel,
                    100.0 * os.stat(local_path).st_blocks * 512 / max(1, local_size))
        _safe_remove(local_path)
        exists, local_size = False, 0

    if exists and expected_size is not None and abs(local_size - expected_size) <= slack:
        if entry and entry.get("size") == local_size and not verify_all:
            _record(manifest, rel, item, local_size, entry.get("sha256"))
            return "skip-known-good", 0
        digest = sha256_file(local_path)
        if entry and entry.get("sha256") == digest:
            _record(manifest, rel, item, local_size, digest)
            return "skip-verified", 0
        if entry and entry.get("sha256") != digest:
            log.warning("Corruption detected for %s (hash mismatch), re-downloading", rel)
        else:
            _record(manifest, rel, item, local_size, digest)
            return "baseline-recorded", 0

    part_path = local_path + ".part"
    part_size = os.path.getsize(part_path) if os.path.exists(part_path) else 0

    if dry_run:
        resuming = expected_size and (0 < local_size < expected_size or 0 < part_size < expected_size)
        action = "would-resume" if resuming else "would-download"
        log.info("[dry-run] %s -> %s", action, rel)
        return action, 0

    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    # Only the bytes still needed matter for the space check (resuming a .part).
    resumable = host_supports_ranges(item.url) and expected_size and part_size < expected_size
    needed = (expected_size - part_size) if resumable else (expected_size or 0)
    if expected_size and not has_free_space(os.path.dirname(local_path) or ".", needed + slack):
        log.error("Insufficient free space for %s (needs ~%d bytes)", rel, needed)
        return "error-diskfull", 0

    try:
        download_atomic(item.url, local_path, expected_size, slack)
    except CurlError as exc:
        log.error("Download failed for %s: %s", item.url, exc)
        return "error", 0

    final_size = os.path.getsize(local_path)
    if expected_size is not None and abs(final_size - expected_size) > slack:
        log.error("Size mismatch after download for %s (got %d, expected %d +/- %d)",
                  rel, final_size, expected_size, slack)
        return "error", 0

    # Record size and mtime now so an interrupted run still gets the fast path
    # next time, and hash off the critical path - re-reading a 13 GB file would
    # otherwise hold a download slot for minutes after the transfer finished.
    _record(manifest, rel, item, final_size, None)
    schedule_hash(manifest, rel, local_path, item, final_size)
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
                            discovery_event: threading.Event | None = None,
                            infocon_candidates: list[dict] | None = None,
                            stop_event: threading.Event | None = None) -> int:
    """Run the DEF CON torrent fetcher in-process so the single-entry workflow remains simple.

    When *infocon_candidates* is provided (from scan_infocon_tree), the torrent
    helper skips its own infocon.org traversal and merges these pre-discovered
    candidates instead, eliminating the duplicate directory scan.
    """
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
        seed_upload_slots=max(0, args.torrent_seed_upload_slots),
        seed_rate_limit_kib=max(0, args.torrent_seed_rate_limit),
        max_seeding=max(1, args.torrent_max_seeding),
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
        status_lines=max(0, args.torrent_status_lines),
        resume_save_minutes=max(1, args.torrent_resume_save_minutes),
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
                     infocon_candidates=infocon_candidates,
                     index_path=args.torrent_index or os.path.join(
                         os.path.expanduser("~"), ".cache", "infocon-scraper", "torrent-index.json"
                     ),
                     stop_event=stop_event,
                     index_ttl_hours=max(0, args.torrent_index_ttl_hours),
                     checkpoint_path=args.torrent_discovery_checkpoint or os.path.join(
                         os.path.expanduser("~"), ".cache", "infocon-scraper", "torrent-discovery-checkpoint.json"
                     ))


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
                   rel_path=os.path.join("mirrors", entry["name"]),
                   modified=entry["modified"], size=entry.get("size"),
                   size_slack=entry.get("size_slack", 0))
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


def resolve_stalled_fallback_root(spec, dest_root: str, media_root: str) -> tuple[str, str] | None:
    """Narrowest HTTP root holding a stalled torrent's content, or None.

    A published .torrent sits *beside* the folder it describes, so the torrent
    URL's parent directory is emphatically not the fallback root: for
    'cons/2600 archive v1 - infocon.org.torrent' that parent is the whole of
    cons/, and crawling it once per stalled torrent multiplies the entire
    conference tree by the number of stalled torrents. The content lives in the
    sibling folder named after the archive, which is what this returns.

    The folder name is derived offline from the torrent name. A wrong guess
    costs one failed directory listing and nothing else, which is cheaper and
    far safer than falling back to a broader root.
    """
    from fetch_defcon_torrents import torrent_content_folder

    if re.search(r"\bdef con\b", spec.name, re.IGNORECASE):
        return build_defcon_media_fallback_root(media_root, spec.name)

    folder = torrent_content_folder(spec.name)
    if not folder or folder in (os.curdir, os.pardir) or "/" in folder or "\\" in folder:
        # Anything but a single plain directory name would resolve above the
        # torrent's own folder - at worst to the site root - so refuse it.
        log.warning("No usable content folder for stalled torrent %s (derived %r); skipping HTTP fallback.",
                    spec.name, folder)
        return None
    parent_rel = os.path.relpath(spec.save_path, dest_root)
    if parent_rel == os.curdir or parent_rel.startswith(os.pardir):
        parent_rel = ""
    parent_url = urljoin(spec.url, "./")
    child_url = urljoin(parent_url, quote(folder) + "/")
    child_rel = os.path.join(parent_rel, folder) if parent_rel else folder
    return child_url, child_rel


def root_is_covered(rel_path: str, planned_rels: list[str]) -> bool:
    """True when some already-planned HTTP root contains *rel_path*.

    Stalled non-DEF CON torrents normally need no fallback crawl at all: their
    conference folder is already part of the ordinary infocon.org sync.
    """
    target = rel_path.replace(os.sep, "/").strip("/").lower()
    for planned in planned_rels:
        candidate = planned.replace(os.sep, "/").strip("/").lower()
        if not candidate:
            return True
        if target == candidate or target.startswith(candidate + "/"):
            return True
    return False


def scan_infocon_tree(
    http_roots: list[tuple[str, str]],
    extra_torrent_roots: list[tuple[str, str]],
    crawl_workers: int,
    dest_root: str,
    stop_requested: threading.Event | None = None,
    stats: ProgressStats | None = None,
    listing_retry_delay: float | None = None,
) -> tuple[list[RemoteFile], list[dict]]:
    """Single-pass recursive scan over infocon.org roots.

    Crawls *http_roots* for both HTTP-downloadable files and .torrent metadata,
    and *extra_torrent_roots* for .torrent metadata only (those paths are served
    via a different HTTP source, e.g. media.defcon.org).  Returns
    *(http_files, torrent_candidates)* so that --with-torrents mode can feed
    both the BitTorrent engine and the HTTP sync worker pool from one network
    traversal instead of two separate ones.

    torrent_candidates entries: {"url": str, "name": str, "save_path": str}
    where save_path is the absolute directory to hand to libtorrent.
    """
    http_files: list[RemoteFile] = []
    torrent_candidates: list[dict] = []
    directories_scanned = 0
    listing_errors = 0
    scheduled_retries = 0
    last_progress = time.time()
    scan_started = time.time()
    retry_delay = RUN.listing_retry_delay if listing_retry_delay is None else max(0.0, listing_retry_delay)

    http_url_set = {url for url, _ in http_roots}

    with ThreadPoolExecutor(max_workers=max(1, crawl_workers)) as pool:
        # (future) -> (url, rel, is_http_eligible, failed_attempts)
        pending: dict = {}
        retry_queue: list[tuple[float, int, str, str, bool, int]] = []
        retry_sequence = 0

        def submit_listing(url: str, rel: str, is_http: bool, failed_attempts: int) -> None:
            pending[pool.submit(list_directory, url)] = (url, rel, is_http, failed_attempts)

        for url, rel in http_roots:
            submit_listing(url, rel, True, 0)
        for url, rel in extra_torrent_roots:
            if url not in http_url_set:
                submit_listing(url, rel, False, 0)

        while pending or retry_queue:
            if stop_requested and stop_requested.is_set():
                break
            now = time.time()
            while retry_queue and retry_queue[0][0] <= now:
                _, _, url, rel, is_http, failed_attempts = heapq.heappop(retry_queue)
                submit_listing(url, rel, is_http, failed_attempts)
            if not pending:
                time.sleep(min(0.25, max(0.0, retry_queue[0][0] - time.time())))
                continue

            retry_wait = None
            if retry_queue:
                retry_wait = max(0.0, retry_queue[0][0] - time.time())
            done, _ = wait(list(pending.keys()), timeout=retry_wait, return_when=FIRST_COMPLETED)
            for fut in done:
                url, rel, is_http, failed_attempts = pending.pop(fut)
                try:
                    entries = fut.result()
                except Exception as exc:  # noqa: BLE001
                    if failed_attempts < RUN.listing_retries:
                        failed_attempts += 1
                        delay = retry_delay * (2 ** (failed_attempts - 1))
                        retry_sequence += 1
                        heapq.heappush(
                            retry_queue,
                            (time.time() + delay, retry_sequence, url, rel, is_http, failed_attempts),
                        )
                        scheduled_retries += 1
                        log.warning(
                            "Directory listing failed for %s; retry %d/%d in %.1fs: %s",
                            url, failed_attempts, RUN.listing_retries, delay, exc,
                        )
                    else:
                        listing_errors += 1
                        log.error(
                            "Failed to list %s during shared inventory scan after %d retries: %s",
                            url, RUN.listing_retries, exc,
                        )
                    continue
                directories_scanned += 1
                for entry in entries:
                    child_url = urljoin(url, entry["href"])
                    child_rel = os.path.join(rel, entry["name"]) if rel else entry["name"]
                    if entry["is_dir"]:
                        submit_listing(child_url, child_rel, is_http, 0)
                    elif entry["name"].lower().endswith(".torrent"):
                        torrent_candidates.append({
                            "url": child_url,
                            "name": entry["name"],
                            # Absolute directory path libtorrent will use as save_path
                            "save_path": os.path.join(dest_root, rel),
                        })
                    elif is_http:
                        http_files.append(RemoteFile(url=child_url, rel_path=child_rel,
                                                    modified=entry["modified"],
                                                    size=entry.get("size"),
                                                    size_slack=entry.get("size_slack", 0)))
                        if stats:
                            stats.add_discovered()
                now = time.time()
                if now - last_progress >= 10:
                    last_progress = now
                    log.info(
                        "Shared inventory progress: %d directories scanned, %d pending, %d retrying, %d HTTP files, %d torrent candidates, %d retries, %d errors, %s elapsed",
                        directories_scanned, len(pending), len(retry_queue), len(http_files), len(torrent_candidates),
                        scheduled_retries, listing_errors, format_duration(now - scan_started),
                    )

    log.info(
        "Shared infocon.org scan complete: %d HTTP files, %d torrent candidates across %d root(s); %d retries, %d errors.",
        len(http_files), len(torrent_candidates), len(http_roots) + len(extra_torrent_roots),
        scheduled_retries, listing_errors,
    )
    return http_files, torrent_candidates


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
    parser.add_argument("--dest", default=None,
                        help="Local destination root (e.g. your InfoCon drive mount point). "
                             "Required except with --ddv-list.")
    parser.add_argument("--base-url", default=DEFAULT_ROOT_URL, help="Root URL of the InfoCon.org site")
    parser.add_argument("--defcon-media-url", default=DEFCON_MEDIA_ROOT_URL,
                         help="Root URL of media.defcon.org (DEF CON's own authoritative media server)")
    parser.add_argument("--sources", default="infocon,defcon-media",
                        help="Comma-separated sources to sync: 'infocon' (everything on infocon.org), "
                            "'mirrors' (only infocon.org/mirrors), and/or 'defcon-media' (media.defcon.org). "
                            "Default: infocon,defcon-media. DEF CON folders that already have a torrent are "
                            "auto-skipped (grab those with fetch_defcon_torrents.py); HTTP crawls everything "
                            "else, including the torrentless remainder such as DEF CON 34.")
    parser.add_argument("--ddv-list", action="store_true",
                         help="Print the DEF CON Data Duplication Village source-drive profiles - every "
                              "drive, the datasets it carries, their sizes, and whether they still fit the "
                              "drive they are nominally sold for - then exit")
    parser.add_argument("--ddv", default=None,
                         help="Comma-separated DDV drive letters to rebuild, e.g. 'A' or 'B,C'. Selects "
                              "exactly the datasets that drive carries and pre-flights the free space at "
                              "--dest. See --ddv-list.")
    parser.add_argument("--ddv-dataset", default=None,
                         help="Comma-separated individual DDV datasets to rebuild, e.g. 'md5,ntlm', for "
                              "mixing and matching across drives. Combines with --ddv. See --ddv-list.")
    parser.add_argument("--ddv-no-preflight", action="store_true",
                         help="Skip the DDV free-space pre-flight check (it otherwise refuses to start a "
                              "transfer that cannot fit)")
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
    parser.add_argument("--metadata-connections", type=int, default=0,
                         help="Per-host cap on concurrent directory listings and HEAD requests; "
                              "0 keeps the built-in per-host defaults (infocon.org 4, media.defcon.org 6)")
    parser.add_argument("--transfer-connections", type=int, default=0,
                         help="Per-host cap on concurrent file downloads, budgeted separately from "
                              "metadata so large transfers cannot starve discovery; 0 keeps the defaults")
    parser.add_argument("--large-file-gib", type=float, default=1.0,
                         help="Files at least this large (per the directory listing) are scheduled under "
                              "the large-transfer budget (default: 1)")
    parser.add_argument("--max-large-downloads", type=int, default=2,
                         help="Maximum concurrent large transfers, so a handful of multi-gigabyte "
                              "archives cannot take every download slot (default: 2)")
    parser.add_argument("--hash-workers", type=int, default=2,
                         help="Background workers that SHA-256 completed downloads; 0 hashes inline "
                              "in the download worker (default: 2)")
    parser.add_argument("--status-interval", type=float, default=10.0,
                         help="Seconds between progress/speed status lines (default: 10; 0 disables)")
    parser.add_argument("--content-order", choices=("newest", "oldest"), default="newest",
                         help="Directory/file discovery order (default: newest)")
    parser.add_argument("--retries", type=int, default=4,
                         help="Per-file download attempts before giving up (default: 4)")
    parser.add_argument("--download-timeout", type=int, default=0,
                         help="Hard wall-clock cap in seconds for a single download attempt; 0 disables it "
                              "(default: 0). A fixed cap cannot fit this archive - the largest word lists "
                              "need many hours - and an aborted attempt restarts from the beginning, so "
                              "stalls are detected with --min-speed-bytes/--stall-timeout instead.")
    parser.add_argument("--min-speed-bytes", type=int, default=1024,
                         help="Abort a download averaging less than this many bytes/s for --stall-timeout "
                              "seconds (default: 1024)")
    parser.add_argument("--stall-timeout", type=int, default=300,
                         help="Seconds below --min-speed-bytes before a download is treated as stalled "
                              "(default: 300)")
    parser.add_argument("--listing-retries", type=int, default=3,
                        help="Additional directory-listing retries after curl gives up (default: 3)")
    parser.add_argument("--listing-retry-delay", type=float, default=5.0,
                        help="Base seconds for exponential directory-listing retry backoff (default: 5)")
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
    parser.add_argument("--torrent-status-lines", type=int, default=10,
                         help="Per-torrent detail lines logged each poll; the rest are summarised (default: 10)")
    parser.add_argument("--torrent-resume-save-minutes", type=int, default=5,
                         help="Minutes between fast-resume checkpoints so a restart skips re-hashing "
                              "already-verified content (default: 5)")
    parser.add_argument("--torrent-seed-time", type=int, default=60,
                         help="If --with-torrents is set, minutes to keep sharing each archive after it "
                              "completes; 0 stops seeding immediately, negative seeds until the run is "
                              "stopped (default: 60)")
    parser.add_argument("--torrent-seed-upload-slots", type=int, default=4,
                         help="How many peers may download from you at once (default: 4)")
    parser.add_argument("--torrent-seed-rate-limit", type=int, default=0,
                         help="Upload cap in KiB/s while seeding; 0 is unlimited (default: 0)")
    parser.add_argument("--torrent-max-seeding", type=int, default=20,
                         help="Maximum archives seeding at once (default: 20)")
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
    parser.add_argument("--torrent-discovery-checkpoint", default=None,
                         help="Resumable recursive torrent discovery checkpoint path")
    parser.add_argument("--manifest", default=None,
                         help="Path to the verification manifest database "
                              "(default: <dest>/.infocon_manifest.db). An existing "
                              ".infocon_manifest.json from an earlier version is imported once.")
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

    # --ddv-list is a catalog query, not a transfer, so it must work without a
    # destination drive attached.
    if args.ddv_list:
        print(ddv_profiles.format_catalog())
        return 0

    if not args.dest:
        parser.error("--dest is required (except with --ddv-list)")

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

    manifest_path = args.manifest or os.path.join(args.dest, ".infocon_manifest.db")
    log_file = args.log_file or os.path.join(args.dest, "infocon_scraper.log")
    os.makedirs(args.dest, exist_ok=True)

    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError as exc:
        print(f"Warning: could not open log file {log_file}: {exc}", file=sys.stderr)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    RUN.retries = max(1, args.retries)
    RUN.download_timeout = max(0, args.download_timeout)
    RUN.min_speed_bytes = max(0, args.min_speed_bytes)
    RUN.stall_seconds = max(1, args.stall_timeout)
    RUN.listing_retries = max(0, args.listing_retries)
    RUN.listing_retry_delay = max(0.0, args.listing_retry_delay)
    RUN.min_free_bytes = max(0, args.min_free_gib) * (1 << 30)
    RUN.content_order = args.content_order
    RUN.metadata_connections = max(0, args.metadata_connections)
    RUN.transfer_connections = max(0, args.transfer_connections)
    RUN.hash_workers = max(0, args.hash_workers)
    RUN.large_file_bytes = max(0, int(args.large_file_gib * (1 << 30)))
    RUN.max_large_downloads = max(1, args.max_large_downloads)

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
    ddv_roots: list[str] = []

    if args.ddv or args.ddv_dataset:
        # A DDV profile IS the selection. Letting it silently merge with a
        # hand-rolled --only-top would produce a drive that matches neither.
        conflicting = [name for name, value in (("--only-cons", args.only_cons),
                                                ("--only-top", args.only_top),
                                                ("--only-mirrors", args.only_mirrors))
                       if value]
        if conflicting:
            log.error("--ddv/--ddv-dataset already select content; remove %s or drop the DDV flags.",
                      " and ".join(conflicting))
            return 2
        try:
            selected = ddv_profiles.resolve(
                [d.strip() for d in args.ddv.split(",") if d.strip()] if args.ddv else None,
                [d.strip() for d in args.ddv_dataset.split(",") if d.strip()] if args.ddv_dataset else None,
            )
        except KeyError as exc:
            log.error("%s", exc.args[0])
            return 2

        for line in ddv_profiles.format_plan(selected, args.dest).splitlines():
            log.info("%s", line)

        if not args.ddv_no_preflight:
            ok, message = ddv_profiles.preflight(selected, args.dest)
            if not ok:
                log.error("Refusing to start: %s", message)
                log.error("Attach a larger drive, drop a dataset, or pass --ddv-no-preflight.")
                return 2

        plan = ddv_profiles.merge_selections(selected)
        sources = list(plan.sources) or sources
        only_top = list(plan.only_top) or None
        only_cons = list(plan.only_cons) or None
        only_mirrors = list(plan.only_mirrors) or None
        # Every hash table is one directory *inside* 'rainbow tables', which
        # --only-top cannot express: selecting Drive B by section would crawl
        # all 22.8 TB of tables instead of its 5.8 TB. The profile's exact
        # paths become the roots directly.
        ddv_roots = list(plan.paths)
        if plan.include_rainbow_tables:
            args.torrent_include_rainbow_tables = True
        if plan.include_mirrors:
            args.torrent_include_mirrors = True

    log.info("Discovering content from sources: %s ...", ", ".join(sources))
    if ddv_roots:
        roots = [(urljoin(args.base_url, quote(path.strip("/")) + "/"), path.strip("/"))
                 for path in ddv_roots]
    else:
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

    inventory_thread: threading.Thread | None = None
    fallback_pool: ThreadPoolExecutor | None = None
    try:
        counts: dict[str, int] = {}
        fallback_counts: dict[str, int] = {}
        fallback_counts_lock = threading.Lock()
        fallback_roots_started: set[str] = set()
        # Every planned HTTP root, so a stalled torrent whose content the sync is
        # already fetching does not trigger a redundant second crawl of it.
        planned_rels = [rel for _, rel in roots]
        http_plan_finished = threading.Event()
        if args.with_torrents:
            torrent_only = only_cons or None
            torrent_ready = threading.Event()
            torrent_discovery = threading.Event()

            # ------------------------------------------------------------------
            # Shared infocon.org inventory discovers regular HTTP files while the
            # torrent helper performs its own torrent-first recursive discovery.
            # Running both traversals concurrently allows torrent metadata and
            # downloads to start without waiting for the HTTP inventory.
            #
            # extra_torrent_roots covers infocon.org paths that are not in
            # non_defcon_roots (those go via media.defcon.org for HTTP) but do
            # publish .torrent files that belong in the torrent inventory.
            # ------------------------------------------------------------------
            extra_torrent_roots: list[tuple[str, str]] = []
            if "infocon" in sources:
                # cons/DEF CON/ on infocon.org has .torrent files; not in HTTP roots
                # (media.defcon.org is the authoritative HTTP source for DEF CON)
                extra_torrent_roots.append((
                    urljoin(args.base_url, "cons/DEF%20CON/"), "cons/DEF CON"
                ))
            if args.torrent_include_mirrors and not any(
                "mirrors" in rel.lower() for _, rel in non_defcon_roots
            ):
                extra_torrent_roots.append((urljoin(args.base_url, "mirrors/"), "mirrors"))
            if args.torrent_include_rainbow_tables and not any(
                "rainbow" in rel.lower() for _, rel in non_defcon_roots
            ):
                extra_torrent_roots.append((
                    urljoin(args.base_url, quote("rainbow tables") + "/"), "rainbow tables"
                ))

            log.info(
                "Running shared infocon.org directory inventory: %d HTTP roots, %d torrent-only roots ...",
                len(non_defcon_roots), len(extra_torrent_roots),
            )
            shared_http_files: list[RemoteFile] = []
            shared_inventory_ready = threading.Event()

            def run_shared_inventory() -> None:
                try:
                    files, _ = scan_infocon_tree(
                        non_defcon_roots, extra_torrent_roots,
                        args.crawl_workers, args.dest, stop_requested, stats,
                    )
                    shared_http_files.extend(files)
                except Exception:
                    log.exception("Shared infocon.org inventory failed unexpectedly.")
                finally:
                    shared_inventory_ready.set()

            def run_stalled_http_fallback(spec) -> None:
                try:
                    root = resolve_stalled_fallback_root(spec, args.dest, args.defcon_media_url)
                except Exception:  # noqa: BLE001 - one bad spec must not kill the fallback queue
                    log.exception("Could not resolve an HTTP fallback root for %s", spec.name)
                    return
                if root is None:
                    return
                url, rel = root
                if not http_plan_finished.is_set() and root_is_covered(rel, planned_rels):
                    log.info("Torrent %s is stalled; %s is already in the HTTP sync plan, "
                             "no extra crawl needed.", spec.name, rel)
                    return
                with fallback_counts_lock:
                    if rel in fallback_roots_started:
                        log.info("Torrent %s is stalled; an HTTP fallback for %s is already running.",
                                 spec.name, rel)
                        return
                    fallback_roots_started.add(rel)
                log.warning("Torrent %s is stalled; starting HTTP fallback for %s", spec.name, rel)
                result = run_sync(
                    [(url, rel)], args.dest, manifest, args.crawl_workers, args.workers,
                    args.verify_all, args.dry_run, stop_requested, [],
                    args.max_pending_downloads, stats
                )
                with fallback_counts_lock:
                    for key, value in result.items():
                        fallback_counts[key] = fallback_counts.get(key, 0) + value

            def start_stalled_http_fallback(spec) -> None:
                # One shared, serialized queue. A thread per stalled torrent used
                # to spawn a full crawl+download pool each, which at ~240 stalled
                # torrents meant ~900 threads all contending for the same handful
                # of per-host connection slots.
                try:
                    fallback_pool.submit(run_stalled_http_fallback, spec)
                except RuntimeError:
                    log.warning("Shutting down; skipping HTTP fallback for %s", spec.name)

            def run_torrent_phase() -> None:
                try:
                    torrent_result[0] = run_defcon_torrent_step(
                        args.dest, torrent_only, args, ready_event=torrent_ready, skip=skip_recent,
                        stalled_callback=start_stalled_http_fallback,
                        discovery_event=torrent_discovery,
                        stop_event=stop_requested,
                    )
                except Exception:
                    log.exception("DEF CON torrent phase failed unexpectedly.")
                    torrent_result[0] = 1
                finally:
                    torrent_discovery.set()
                    torrent_ready.set()

            # Serialized so that N stalled torrents cost one crawl at a time,
            # not N concurrent crawl+download pools.
            fallback_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="http-fallback")
            torrent_thread = threading.Thread(
                target=run_torrent_phase, name="defcon-torrents", daemon=False
            )
            torrent_thread.start()
            inventory_thread = threading.Thread(
                target=run_shared_inventory, name="shared-infocon-inventory", daemon=False
            )
            inventory_thread.start()
            log.info("Torrent discovery and shared HTTP inventory are running in parallel ...")
            torrent_discovery.wait()
            log.info("Online torrent inventory complete; waiting for shared HTTP inventory ...")
            shared_inventory_ready.wait()
            if shared_http_files or non_defcon_roots:
                log.info("Online torrent inventory complete; starting non-DEF CON HTTP sync ...")
                # non_defcon_roots already inventoried by shared scan; pass as initial_files
                # so run_sync downloads immediately without a second crawl pass.
                for key, value in run_sync(
                    [], args.dest, manifest, args.crawl_workers, args.workers,
                    args.verify_all, args.dry_run, stop_requested, shared_http_files,
                    args.max_pending_downloads, stats, initial_files_already_counted=True
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
            # From here on nothing else is scheduled to fetch this content over
            # HTTP, so a stalled torrent's fallback is no longer redundant.
            http_plan_finished.set()
        else:
            counts = run_sync(roots, args.dest, manifest, args.crawl_workers, args.workers,
                              args.verify_all, args.dry_run, stop_requested, initial_files,
                              args.max_pending_downloads, stats)
    finally:
        if inventory_thread:
            inventory_thread.join()
        if torrent_thread:
            torrent_thread.join()
        http_plan_finished.set()
        if fallback_pool:
            # The torrent thread has stopped, so no further fallbacks can be
            # queued; drain whatever it handed over.
            fallback_pool.shutdown(wait=True)
        drain_hashes()
        if reporter:
            reporter.stop()
            reporter.join(timeout=args.status_interval + 2)
        manifest.close()
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
