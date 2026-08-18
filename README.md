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

The public DDV historical briefings identify the five-source model used for recent DEF CON duplications as:

1. InfoCon.org archive
2. Rainbow Tables 1 of 3
3. Rainbow Tables 2 of 3
4. Rainbow Tables 3 of 3
5. VX Underground Archive

That means the `mirrors/vx underground - 2025 June/` collection is directly relevant to recreating a DDV source drive. The other mirror collections listed above are useful independent archives, but they are not identified in the DDV briefings as part of those five source drives. The Rainbow Tables source is under the normal top-level `rainbow tables/` section, not under `mirrors/`.

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

# Use only InfoCon or only the DEF CON media server
python infocon_scraper.py --dest "/path/to/drive" --sources infocon
python infocon_scraper.py --dest "/path/to/drive" --sources defcon-media

# Exclude folders being handled by torrents
python infocon_scraper.py --dest "/path/to/drive" \
  --defcon-media-skip "DEF CON 30,DEF CON 31,DEF CON 32,DEF CON 33"

# Verify existing files without trusting size-only matches
python infocon_scraper.py --dest "/path/to/drive" --verify-all

# Inspect actions without downloading
python infocon_scraper.py --dest "/path/to/drive" --dry-run
```

The HTTP scraper uses separate directory-listing and download worker pools. It streams work as directories are discovered, so later large trees do not block priority content. Requests to `media.defcon.org` are capped to avoid server throttling.

Tune HTTP concurrency for a particular machine or network:

```bash
# More directory listings, fewer downloads
python infocon_scraper.py --dest "/path/to/drive" \
  --crawl-workers 24 --workers 4

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

## Safety Notes

- The tools do not delete remote or local archive content automatically.
- Keep sufficient free space for the largest archive you intend to fill.
- Stop the download processes before unmounting or resizing a destination disk.
- Validate that an NTFS destination is mounted read-write before starting.
- Respect the archive host's terms, bandwidth limits, and applicable law.