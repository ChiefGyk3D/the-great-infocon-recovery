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
import time

import libtorrent as lt

TORRENTS_DIR_URL = "https://media.defcon.org/DEF%20CON%20Torrents/"
USER_AGENT = "InfoConDriveSync/1.0 (personal archive sync tool)"
DEFAULT_TORRENTS_CACHE = os.environ.get(
    "INFOCON_TORRENTS_CACHE",
    os.path.join(os.path.expanduser("~"), ".cache", "infocon-scraper", "torrents"),
)


def curl_text(url: str) -> str:
    proc = subprocess.run(
        ["curl", "-sS", "-A", USER_AGENT, "--retry", "3", "--retry-delay", "3",
         "--retry-all-errors", "-L", "--fail", url],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed ({proc.returncode}) for {url}: {proc.stderr.strip()}")
    return proc.stdout


def curl_download(url: str, local_path: str) -> None:
    proc = subprocess.run(
        ["curl", "-sS", "-A", USER_AGENT, "--retry", "3", "--retry-delay", "3",
         "--retry-all-errors", "-L", "--fail", "-o", local_path, url],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed ({proc.returncode}) for {url}: {proc.stderr.strip()}")


def discover_torrents(dir_url: str) -> dict[str, str]:
    """Return {base_name: href} picking the highest 'vN' version per base name."""
    html = curl_text(dir_url)
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


def fetch_all(dest: str, torrents_dir: str, only: list[str] | None) -> int:
    os.makedirs(dest, exist_ok=True)
    os.makedirs(torrents_dir, exist_ok=True)

    available = discover_torrents(TORRENTS_DIR_URL)
    if only:
        filters = [f.lower() for f in only]
        available = {name: href for name, href in available.items()
                     if any(f in name.lower() for f in filters)}
    if not available:
        print("No matching torrents found.")
        return 1

    print(f"Found {len(available)} torrents to fetch/verify.")
    ses = lt.session({"listen_interfaces": "0.0.0.0:6881"})
    handles = {}

    for name, href in sorted(available.items()):
        torrent_url = TORRENTS_DIR_URL + href
        torrent_path = os.path.join(torrents_dir, f"{name}.torrent")
        if not os.path.exists(torrent_path):
            print(f"Fetching torrent metadata for {name} ...")
            curl_download(torrent_url, torrent_path)
        info = lt.torrent_info(torrent_path)
        h = ses.add_torrent({"ti": info, "save_path": dest})
        handles[name] = h
        print(f"Added {name}: {info.total_size() / 1e9:.2f} GB, {info.num_files()} files")

    print("Verifying/downloading... (Ctrl+C to stop; already-correct files are skipped automatically)")
    try:
        while True:
            done = 0
            active_lines = []
            for name, h in handles.items():
                s = h.status()
                if s.state in (lt.torrent_status.seeding, lt.torrent_status.finished):
                    done += 1
                else:
                    active_lines.append(
                        f"{name}: {s.progress * 100:5.1f}%  down {s.download_rate / 1e6:6.2f} MB/s  "
                        f"peers {s.num_peers}  state {s.state}"
                    )
            print(f"--- {done}/{len(handles)} complete ---")
            for line in active_lines[:10]:
                print(line)
            if done == len(handles):
                print("All requested DEF CON items fully downloaded and verified.")
                break
            time.sleep(10)
    except KeyboardInterrupt:
        print("Interrupted - already-verified pieces are safe; re-run to resume/continue.")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch/verify DEF CON archives via BitTorrent v2 (libtorrent).")
    parser.add_argument("--dest", required=True, help="Destination cons/DEF CON directory")
    parser.add_argument("--only", default=None,
                         help="Comma-separated substrings to restrict which items are fetched "
                              "(default: all available torrents)")
    parser.add_argument("--torrents-dir", default=DEFAULT_TORRENTS_CACHE,
                         help=f"Where to cache .torrent files (default: {DEFAULT_TORRENTS_CACHE})")
    args = parser.parse_args()

    only = [f.strip() for f in args.only.split(",") if f.strip()] if args.only else None
    return fetch_all(args.dest, args.torrents_dir, only)


if __name__ == "__main__":
    sys.exit(main())

