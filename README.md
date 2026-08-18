# The Great InfoCon Recovery

The Great InfoCon Recovery rebuilds and maintains a local time capsule of the [InfoCon.org](https://infocon.org/) archive and DEF CON's [media server](https://media.defcon.org/). It preserves the source directory layout, skips files already verified locally, resumes partial downloads, and records SHA-256 hashes in a manifest.

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE).

## What It Does

`infocon_scraper.py` mirrors:

- All conferences under `infocon.org/cons/`, with DEF CON and BSides submitted first.
- `documentaries/`, `podcasts/`, `rainbow tables/`, `skills/`, and `word lists/`.
- Selected folders from `media.defcon.org`, normally DEF CON content that is missing from InfoCon.
- Nested files into their corresponding source folders. It does not flatten archives into a new top-level namespace.

The enormous `mirrors/` tree is excluded by default because repositories such as vx-underground can consume an entire disk. Include it explicitly with `--only-top mirrors`.

### InfoCon Mirrors

`https://infocon.org/mirrors/` contains independent historical or community archive collections. The current top-level listing includes:

| Collection | Approximate listed size | Transfer options |
| --- | ---: | --- |
| Cryptome.org snapshot | 85.7 GiB | HTTP or v1/v2 archive torrent |
| Gutenberg.net.au snapshot | 8.1 GiB | HTTP or v1/v2 archive torrent |
| textfiles.com 2011 | 13.0 GiB | HTTP or torrent |
| Internet Census 2012 | Directory plus v1/v2 torrents | HTTP or torrent |
| PoCGTFO | Directory | HTTP |
| vx-underground 2024/2025 | Very large malware-sample archives | Torrent or HTTP; plan storage carefully |
| hackcanada.com | 641.8 MiB | HTTP |
| nettwerked.net | 1023.3 MiB | HTTP |

Sizes and availability can change. Check the live listing before starting a large transfer. The mirror archives are independent collections, not alternate copies of the normal `cons/` conference folders.

### DDV Source-Drive Relationship

The current six-drive DDV layout is:

| Drive | Capacity | Contents | This project's coverage |
| --- | ---: | --- | --- |
| A | 6 TB | 2026 InfoCon.org archive | Primary target of `infocon_scraper.py` |
| B | 6 TB | Lanman, MSSQL, and NTLM hash tables | Separate companion dataset |
| C | 6 TB | A5/1, GSM, and MD5 hash tables | Separate companion dataset |
| D | 8 TB | VX Underground archive, latest papers, samples, and code | Available through the relevant `mirrors/vx underground .../` collections |
| E | 8 TB | NTLM-9 hash tables | Separate companion dataset |
| F | 6 TB | 4.2 Net NTLMv1 rainbow table | Separate companion dataset |

Drive A is the InfoCon archive this project is designed to build. Drive D's VX Underground material is represented by the relevant mirror collections described below. Drives B, C, E, and F are separate hash-table datasets and are not automatically supplied by `infocon_scraper.py`; keep them in their own destination trees rather than combining them with the InfoCon mirror.

The normal top-level `rainbow tables/` section may contain related material, but it should not be treated as a complete replacement for Drives B, C, E, or F without checking the source inventory.

To target the DDV-related mirror collection without crawling every conference:

```bash
python infocon_scraper.py --dest "/path/to/drive" \
  --sources mirrors --only-mirrors "vx underground"
```

To acquire the DDV-style InfoCon plus Rainbow Tables sources in separate destination trees, run separate jobs or use separate destination roots. Do not combine them into one flat folder if the goal is to reproduce the source-drive layout.

Download one or more selected mirror collections while preserving their nested layout:

```bash
# One collection
python infocon_scraper.py --dest "/path/to/drive" \
  --sources mirrors --only-mirrors cryptome

# Several collections by case-insensitive substring
python infocon_scraper.py --dest "/path/to/drive" \
  --sources mirrors --only-mirrors "textfiles,gutenberg"

# Preview the selected mirror transfer
python infocon_scraper.py --dest "/path/to/drive" \
  --sources mirrors --only-mirrors cryptome --dry-run
```

`--only-mirrors` filters mirror collection names; `--sources mirrors` prevents the command from also crawling the conference archive. To sync the normal InfoCon archive and selected mirrors in one run, use `--sources infocon --only-mirrors ...`.

Mirror files are written below `mirrors/<collection>/`. For collections with a published `.torrent`, prefer the torrent when available: it provides piece-level verification and resumes efficiently. Do not enable the whole mirrors tree casually:

```bash
# This includes every mirror collection and can require multiple terabytes.
python infocon_scraper.py --dest "/path/to/drive" --only-top mirrors
```

The scraper intentionally does not auto-extract `.rar` archives. It preserves the archive exactly as published and skips an archive when an unpacked folder with the same base name already exists locally, preventing duplicate storage.

`fetch_defcon_torrents.py` discovers the per-archive torrents in `media.defcon.org/DEF%20CON%20Torrents/` and uses `libtorrent` to verify and fill gaps. The torrent's own internal folder name determines the destination, so existing folders are reused instead of duplicated.

## Requirements

- Python 3.10 or newer
- `curl`
- A writable destination disk
- `libtorrent` for the BitTorrent helper

On Debian/Ubuntu-like systems:

```bash
sudo apt install curl python3-venv
```

Create the environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

## HTTP Sync

Run an incremental full sync. Replace the destination with the mount point on your system:

```bash
python infocon_scraper.py \
  --dest "/path/to/InfoCon drive"
```

Useful options:

```bash
# Only specific conference groups
python infocon_scraper.py --dest "/path/to/drive" --only-cons "DEF CON,BSides"

# Only selected top-level sections
python infocon_scraper.py --dest "/path/to/drive" --only-top "documentaries,podcasts"

# Include the large mirrors tree explicitly
python infocon_scraper.py --dest "/path/to/drive" --only-top mirrors

# Default crawls everything else: all of infocon.org plus the torrentless
# DEF CON remainder (e.g. DEF CON 34). DEF CON folders that already have a
# torrent are auto-skipped - grab those with fetch_defcon_torrents.py.
python infocon_scraper.py --dest "/path/to/drive"

# Keep every media.defcon.org folder even if a torrent exists (rarely needed)
python infocon_scraper.py --dest "/path/to/drive" --no-skip-torrented

# InfoCon only, no media.defcon.org at all
python infocon_scraper.py --dest "/path/to/drive" --sources infocon

# Verify existing files without trusting size-only matches
python infocon_scraper.py --dest "/path/to/drive" --verify-all

# Inspect actions without downloading
python infocon_scraper.py --dest "/path/to/drive" --dry-run
```

The HTTP scraper uses separate directory-listing and download worker pools. It streams work as directories are discovered, so later large trees do not block priority content. Requests to `media.defcon.org` are capped to avoid server throttling. Download scheduling is bounded so very large trees do not create hundreds of thousands of in-memory futures.

Tune HTTP concurrency for a particular machine or network:

```bash
# More directory listings, fewer downloads
python infocon_scraper.py --dest "/path/to/drive" \
  --crawl-workers 24 --workers 4

# Bound queued/in-flight downloads; default is workers * 4
python infocon_scraper.py --dest "/path/to/drive" \
  --workers 8 --max-pending-downloads 32

# Fewer connections for a throttled host or slower disk
python infocon_scraper.py --dest "/path/to/drive" \
  --crawl-workers 4 --workers 2
```

## DEF CON Torrents

The per-archive torrents are BitTorrent v2 metadata. Older distro versions of `aria2c` and `transmission-cli` may reject them, so the helper uses Python `libtorrent`.

Fetch and verify every available DEF CON torrent:

```bash
python fetch_defcon_torrents.py \
  --dest "/path/to/drive/cons/DEF CON"
```

Restrict the torrent set:

```bash
python fetch_defcon_torrents.py \
  --dest "/path/to/drive/cons/DEF CON" \
  --only "30,31,32,33"
```

The helper chooses the highest available torrent version for each archive, caches metadata under `~/.cache/infocon-scraper/torrents`, and accepts `--torrents-dir` or the `INFOCON_TORRENTS_CACHE` environment variable to relocate that cache:

```bash
INFOCON_TORRENTS_CACHE="/fast/nvme/infocon-torrents" \
  python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON"
```

Existing files are piece-hash checked. Correct pieces remain in place; missing or altered pieces are downloaded. Stop with `Ctrl+C` and rerun to resume.

The torrent runner is deliberately adjustable:

```bash
# Eight active torrents, 800 total peer connections
python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" \
  --max-active 8 --connections 800

# One active torrent for a constrained connection or disk
python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" \
  --max-active 1 --connections 100 --poll-seconds 30

# Unlimited active torrents, if the machine and network can handle it
python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" \
  --max-active 0

# Tune peer discovery and metadata retry behavior
python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" \
  --no-lsd --request-timeout 300 --retries 5 --retry-delay 10

# Bind BitTorrent to a chosen interface and port
python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" \
  --listen-interface "0.0.0.0:51413"
```

The default torrent status output reports the total rate, number of active torrents, and queued/idle torrents. A torrent showing zero peers can be queued by libtorrent or temporarily have no available peers; it is not automatically an error.

### Combined Torrent and HTTP Mode

To use the torrent-backed DEF CON content as the authoritative source while filling the rest of the archive over HTTP, run:

```bash
python infocon_scraper.py \
  --dest "/path/to/InfoCon drive" \
  --with-torrents
```

The combined workflow adds all available DEF CON torrents and waits for their initial file checking to finish. It then starts the HTTP crawl while libtorrent continues downloading the checked torrent content. HTTP automatically skips DEF CON folders represented by torrents, so the two phases do not intentionally duplicate those archives. A single drive-level lock remains held by the parent process until both phases finish.

DEF CON 34 may not have a torrent yet. Run the HTTP scraper for that year and any special folders not represented by torrents.

> The `infocon_scraper.py --fetch-torrent` option shells out to `aria2c`, which only supports BitTorrent v1. DEF CON's per-archive torrents on `media.defcon.org` are v2, so use `fetch_defcon_torrents.py` (libtorrent) for those.

## Monitoring

Run jobs detached if desired:

```bash
nohup python infocon_scraper.py --dest "/path/to/drive" > run.out 2>&1 &
nohup python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" > torrent.out 2>&1 &
```

Watch them live:

```bash
tail -f run.out
tail -f torrent.out
pgrep -fa 'infocon_scraper.py|fetch_defcon_torrents.py'
df -h "/path/to/drive"
```

The HTTP manifest is stored at `<destination>/.infocon_manifest.json` by default. Logs can be placed elsewhere with `--log-file`.

## Robustness

The HTTP sync is built to survive interruptions and large runs:

- **Atomic writes.** Each file downloads to a `.part` sibling and is renamed into place only after its size matches the server's `Content-Length`. A killed or truncated transfer never leaves a file that looks complete.
- **Per-file retries.** Failed or size-mismatched downloads retry with backoff (`--retries`, default 4). Partial `.part` files resume when the server supports ranges.
- **Bounded memory.** Download scheduling is capped (`--max-pending-downloads`, default `workers * 4`) so very large trees cannot accumulate hundreds of thousands of in-memory tasks.
- **Disk-space guard.** A download that would leave less than `--min-free-gib` (default 1) free is refused, and a genuinely full destination halts the run cleanly instead of writing corrupt files.
- **Periodic manifest saves.** The verification manifest is flushed every 200 completions, so a crash or power loss keeps most hash progress.
- **Single-instance lock.** A `.infocon_scraper.lock` PID file under the destination prevents two syncs from racing on the same drive. Stale locks (dead PID) are reclaimed automatically; override with `--force`.
- **Signal handling.** `SIGINT`/`SIGTERM` finish in-flight downloads, save the manifest, and release the lock.
- **Corruption detection.** A local file whose recorded hash no longer matches is re-downloaded.

Relevant options:

```bash
python infocon_scraper.py --dest "/path/to/drive" \
  --retries 6 --download-timeout 7200 --min-free-gib 5
```

## Safety Notes

- The tools do not delete remote or local archive content automatically.
- Keep sufficient free space for the largest archive you intend to fill.
- Stop the download processes before unmounting or resizing a destination disk.
- Validate that an NTFS destination is mounted read-write before starting.
- Respect the archive host's terms, bandwidth limits, and applicable law.