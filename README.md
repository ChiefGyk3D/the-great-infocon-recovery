# The Great InfoCon Recovery

The Great InfoCon Recovery rebuilds and maintains a local time capsule of the [InfoCon.org](https://infocon.org/) archive and DEF CON's [media server](https://media.defcon.org/). It preserves the source directory layout, skips files already verified locally, resumes partial downloads, and records SHA-256 hashes in a manifest.

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE).

## Contents

- [What It Does](#what-it-does) - scope, mirrors, and the DDV source-drive relationship
- [Requirements](#requirements) - environment, tests, and linting
- [HTTP Sync](#http-sync) - crawling, ordering, concurrency budgets, and scheduling
- [DEF CON Torrents](#def-con-torrents) - the BitTorrent path, [sharing back](#sharing-back), and [verification](#verifying-against-publisher-piece-hashes)
- [Combined Torrent and HTTP Mode](#combined-torrent-and-http-mode) - how the two engines divide the work
- [Monitoring](#monitoring) - logs, the overview daemon, and the tmux dashboard
- [Robustness](#robustness) - every integrity guarantee, and its limits
- [How the Two Engines Cooperate](#how-the-two-engines-cooperate) - ownership, fallback, and why it matters
- [Safety Notes](#safety-notes)

## What It Does

`infocon_scraper.py` mirrors:

- All conferences under `infocon.org/cons/`, with DEF CON and BSides submitted first.
- `documentaries/`, `podcasts/`, `skills/`, and `word lists/` by default.
- `rainbow tables/` only when explicitly requested; it is a separate multi-terabyte dataset.
- Selected folders from `media.defcon.org`, normally DEF CON content that is missing from InfoCon.
- Nested files into their corresponding source folders. It does not flatten archives into a new top-level namespace.

The enormous `mirrors/` tree is excluded by default because repositories such as vx-underground can consume an entire disk. Include it explicitly with `--only-top mirrors`.

`ddv_profiles.py` maps all six DEF CON Data Duplication Village source drives onto the archive, so you can rebuild any of them — or mix datasets across them — without working out the layout yourself. Start with `python infocon_scraper.py --ddv-list`; see [DDV Source-Drive Profiles](#ddv-source-drive-profiles).

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

### DDV Source-Drive Profiles

Every year the DEF CON Data Duplication Village hands out a set of source drives
that attendees queue up to copy. All of that content is published on
infocon.org, so anyone can rebuild any of those drives from home. The hard part
was never access — it was knowing *which* slice of a 37 TB archive belongs on
which drive, and whether what you picked will actually fit.

`ddv_profiles.py` is that map, and both tools can be driven from it directly.

#### Seeing the layout

```bash
python infocon_scraper.py --ddv-list      # no --dest needed; it is a catalog query
```

That prints every drive, the datasets it carries, the publisher's stated size,
the size actually declared by the publisher's torrents, and whether the total
still fits the drive it is nominally sold for:

| Drive | Nominal | Contents | Measured | Fits? |
| --- | ---: | --- | ---: | --- |
| A | 6 TB | InfoCon.org archive | 6.29 TB | **No — over by 378 GB** |
| B | 6 TB | LANMAN + MySQL SHA-1 + NTLM tables | 5.84 TB | Yes (98.8%) |
| C | 6 TB | A5/1 (GSM) + MD5 tables | 5.57 TB | Yes (94.2%) |
| D | 8 TB | vx-underground (2025 June) | 8.47 TB | **No — over by 595 GB** |
| E | 8 TB | NTLM-9 tables | 6.71 TB | Yes (85.2%) |
| F | 6 TB | Net-NTLMv1 table | 4.75 TB | Yes (80.3%) |

Full DDV set: **37.63 TB across 1,048,611 files.**

Sizes are measured from the publisher's own v1/v2 torrents, and capacity
allows 1.5% for filesystem metadata (a drive sold as "6 TB" is 6 × 10¹² bytes,
and a fresh ext4 with `-m 0` gives back a little less than that).

**Two drives no longer fit their nominal capacity.** The archive grew; the drive
sizes did not. This is reported rather than silently trimmed — deciding what to
drop, or reaching for a larger disk, is your call, not the tool's. Drive D ships
an `alternate` for exactly this reason: the 2024 June vx-underground snapshot is
6.38 TB and still fits an 8 TB disk.

#### Rebuilding a drive

```bash
# Drive B: LANMAN + MySQL SHA-1 + NTLM, torrent-first (the publisher asks for this)
python fetch_defcon_torrents.py --dest "/mnt/driveB" --ddv B

# Drive A: the InfoCon archive, HTTP with torrents filling the big trees
python infocon_scraper.py --dest "/mnt/driveA" --ddv A --with-torrents
```

Before a single byte moves, the selection is printed and pre-flighted against
the free space actually available at `--dest`:

```
DDV selection: 1 dataset(s), 6.71 TB, 10,008 files
  drive E  ntlm9                  6.71 TB  NTLM 9 rainbow tables
  INSUFFICIENT SPACE: 6.71 TB required but only 1.68 TB free at /mnt/x - short by 5.04 TB
Refusing to start: ...
Attach a larger drive, drop a dataset, or pass --ddv-no-preflight.
```

#### Mixing and matching

Drives are the convenient unit, but datasets are the real one. Any combination
works, across drive boundaries:

```bash
python infocon_scraper.py --dest "/mnt/mine" --ddv-dataset md5,ntlm,wordlists
```

| Dataset | Drive | Measured | Notes |
| --- | --- | ---: | --- |
| `cons` | A | 3.83 TB | 239 conferences |
| `defcon` | A | 1.77 TB | The DEF CON archive |
| `skills` | A | 312 GB | |
| `wordlists` | A | 225 GB | 25 very large archives |
| `podcasts` | A | 101 GB | |
| `documentaries` | A | 54 GB | |
| `ntlm` | B | 3.96 TB | |
| `mysqlsha1` | B | 1.48 TB | See naming note below |
| `lanman` | B | 398 GB | |
| `md5` | C | 3.89 TB | |
| `a51` | C | 1.68 TB | A5/1 is the GSM cipher |
| `vx-underground` | D | 8.47 TB | 2025 June snapshot |
| `vx-underground-2024` | D | 6.38 TB | Alternate that fits 8 TB |
| `ntlm9` | E | 6.71 TB | 9-character, ~50% effective |
| `net-ntlmv1` | F | 4.75 TB | Compressed from ~8 TB |

Naming a drive and one of its own datasets does not double-count it, and
`--ddv`/`--ddv-dataset` refuse to run alongside `--only-top`, `--only-cons`,
`--only-mirrors` or `--only`: a profile *is* the selection, and letting it merge
with a hand-rolled filter would produce a drive matching neither.

#### Two details worth knowing

**The hash tables live inside `rainbow tables/`, not beside it.** `--only-top`
can only name a whole top-level section, so selecting Drive B that way would
crawl all 22.87 TB of tables instead of its 5.84 TB. Each dataset therefore
carries its exact sub-tree (`rainbow tables/ntlm`), and `NTLM` never selects
`NTLM 9` — matching is done on the publisher's torrent names, which are exact.

**DEF CON is unlinked from its own parent index.** `infocon.org/cons/` lists 239
conference directories and does *not* include DEF CON, even though
`cons/DEF CON/` is directly browsable and holds DEF CON 1 through 34. A plain
`cons/` crawl therefore never descends into it. The `defcon` dataset keeps its
own root for this reason; drop it and Drive A silently loses 1.77 TB.

#### Keep the datasets on separate trees

Drives B, C, E and F are hash tables and are excluded from the default crawl and
torrent inventory because they are a separate multi-terabyte workload. Keep them
in their own destination roots rather than combining them with the InfoCon
mirror — do not place them under Drive A's tree. Drive D's vx-underground
material likewise belongs on its own disk.

If you would rather drive the tools by hand, the underlying flags still work:

```bash
python infocon_scraper.py --dest "/path/to/drive" --only-top "rainbow tables"
python fetch_defcon_torrents.py --dest "/path/to/drive" --include-rainbow-tables
python infocon_scraper.py --dest "/path/to/drive" --sources mirrors --only-mirrors "vx underground"
```

#### A naming discrepancy, surfaced rather than resolved

The DDV drive list names Drive B's middle dataset **MSSQL**. The archive
publishes **`mysqlsha1` / "MySQL SHA-1 rainbow tables"**, and no MSSQL set
exists anywhere in `rainbow tables/`. The profile treats them as the same slot
and says so in `--ddv-list` rather than quietly picking one and hoping.

The publisher's own figures (from `rainbow tables/## READ ME RAINBOW TABLES ##.txt`,
updated 2026-06-07) are A51 1.5 TB, LANMAN 0.4 TB, MD5 3.5 TB, MySQL SHA-1
1.3 TB, NTLM 3.6 TB, NTLM 9 6.7 TB, NetNTLMv1 4.3 TB (compressed from 8 TB with
a 3% RAR recovery record). Measured torrent sizes run consistently a little
higher; both are shown so a divergence stays visible instead of being averaged
away.

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

To run the test suite and linter:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -v
ruff check .
```

## HTTP Sync

### Fresh Builds and Refreshes

The destination may be completely empty. The online InfoCon and media sources are authoritative; existing files are only used to resume and verify. The same command can be rerun later to keep a drive current as new conferences, talks, torrents, and corrections appear.

Use the single friendly entrypoint for normal users:

```bash
./.venv/bin/python bin/infocon.py
```

It opens a numbered wizard for destination, refresh scope, HTTP workers, torrent scope, stalled-torrent fallback, and Rainbow Tables confirmation. For repeat runs, copy `.env.example` to `.env`, edit it once, then use:

```bash
./.venv/bin/python bin/infocon.py --repeat
```

Advanced users can bypass the wizard and use the full scraper CLI:

```bash
./.venv/bin/python bin/infocon.py --advanced --dest "/path/to/drive" --with-torrents
```

Rainbow Tables remain excluded unless explicitly enabled in the wizard or `.env`.

The scraper does not use the existing drive as an inventory and does not require any pre-existing folders, torrent files, or manifest. It creates the destination tree as it discovers the current online InfoCon layout.

Run an incremental full sync. Directory listings are ordered by their published modification metadata, newest first, including nested subdirectories. Replace the destination with the mount point on your system:

```bash
python infocon_scraper.py \
  --dest "/path/to/InfoCon drive"
```

Run the same command again later to refresh the drive. The online directory listings and torrent inventories are checked again; files already verified in the destination manifest are skipped, changed/new files are added, and interrupted `.part` downloads resume. A 2022-era drive is therefore only a resume/cache advantage, never a limit on what the current online scan can discover.

Content discovery defaults to newest-first using the online directory modification metadata, including nested directories. Torrent scheduling also defaults to newest-first. Select `--content-order oldest --torrent-order oldest` for a deliberate historical backfill; the wizard and `.env` expose the same choice.

Listings also publish a rounded file size. Because neither host sends `Content-Length`, this is the only size available before a transfer, and the scraper uses it for scheduling, free-space checks, skip decisions, and post-download verification - always compared within the slack implied by how precisely it was printed.

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

The HTTP scraper uses separate directory-listing and download worker pools. It streams work as directories are discovered, so later large trees do not block priority content. Download scheduling is bounded so very large trees do not create hundreds of thousands of in-memory futures.

Per-host concurrency is capped, with **separate budgets for metadata and transfers**. Directory listings and HEAD requests draw on one budget, file downloads on another, so a handful of multi-gigabyte archives cannot hold every connection and starve discovery on the same host:

```bash
python infocon_scraper.py --dest "/path/to/drive" \
  --metadata-connections 4 --transfer-connections 4
```

Concurrent large transfers are capped separately again. Files at least `--large-file-gib` (default 1) get their own budget of `--max-large-downloads` (default 2) slots, so reaching several huge archives at once cannot consume the whole download pool:

```bash
# Allow three concurrent large transfers, counting anything over 2 GiB as large
python infocon_scraper.py --dest "/path/to/drive" \
  --large-file-gib 2 --max-large-downloads 3
```

Completed downloads are SHA-256 hashed on a small background pool (`--hash-workers`, default 2) rather than in the download worker, since re-reading a multi-gigabyte file would otherwise hold a download slot for minutes after the transfer finished. Set `--hash-workers 0` to hash inline.

The default HTTP settings already use bounded crawl/download pools and host concurrency limits. Increase `--workers` only when the source and disk can handle it; raising it too far can trigger remote connection timeouts rather than improving throughput.

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

Combined mode recursively searches the current online InfoCon tree for `.torrent` files, including nested paths such as `documentaries/Hacker Movies/`, regardless of whether those files or folders exist on the destination drive. It excludes `mirrors/` and `rainbow tables/` by default because those are separate enormous workloads; opt in with `--torrent-include-mirrors` and/or `--torrent-include-rainbow-tables`. Torrent content is saved beneath the matching source-relative destination tree rather than flattened into `cons/DEF CON`.

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

The default torrent status output reports the total rate, number of active torrents, queued/idle torrents, and how many are not yet admitted to the session. A torrent showing zero peers can be queued by libtorrent or temporarily have no available peers; it is not automatically an error.

Per-torrent detail lines are capped at `--status-lines` (default 10, `--torrent-status-lines` in combined mode) and the remainder is summarised by state. Printing every incomplete torrent each poll produced thousands of log lines a minute on a large set.

Torrents are admitted to the session a window at a time rather than all at once, so a large set does not hash-check every archive simultaneously against one disk.

### Sharing Back

A rebuilt drive is a fully populated seed, and the archive is community-hosted - its own Rainbow Tables README asks contributors to "help us grow our archive". Completed archives are therefore shared back by default rather than dropped the moment the last piece lands.

Seeding is bounded by explicit limits so it never competes with the work still in progress:

| Option | Default | Meaning |
| --- | ---: | --- |
| `--seed-time` | 60 | Minutes to keep sharing each archive after it completes. `0` stops immediately; a negative value seeds until the run is stopped. |
| `--seed-upload-slots` | 4 | How many peers may download from you at once. |
| `--seed-rate-limit` | 0 | Upload cap in KiB/s; `0` is unlimited. |
| `--max-seeding` | 20 | Maximum archives seeding concurrently. |

```bash
# Share generously from a machine with spare uplink
python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" \
  --seed-time -1 --seed-upload-slots 12

# Contribute without disturbing anything else on the connection
python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" \
  --seed-rate-limit 2048 --seed-upload-slots 2

# Opt out entirely
python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" --seed-time 0
```

Combined mode exposes the same controls as `--torrent-seed-time`, `--torrent-seed-upload-slots`, `--torrent-seed-rate-limit`, and `--torrent-max-seeding`. Seeding torrents no longer consume download slots: `active_limit` covers downloads and seeds separately, so finishing an archive does not slow the ones still arriving. The status line reports what you are giving back, e.g. `seeding 6 to 14 peer(s) at 3.21 MB/s up`.

Fast-resume data is checkpointed every `--resume-save-minutes` (default 5) and again on exit, including after `Ctrl+C`. Without it every restart re-hash-checks the entire set - terabytes on a populated drive - before anything can transfer. Checkpoints live under the torrent cache directory, so relocating the cache with `--torrents-dir` or `INFOCON_TORRENTS_CACHE` moves them too.

### Verifying Against Publisher Piece Hashes

There are two different questions about a file, and only one of them the HTTP sync can answer.

The manifest's SHA-256 proves a file **has not changed since it was first seen**. It cannot prove the file **matches what was published**, because neither archive host sends `Content-Length` and the directory listing's size is rounded. At gigabyte scale that rounding allows about 107 MB of tolerance, so a materially short file passes every check available over HTTP - and once its hash is recorded from the short copy, `--verify-all` will keep confirming it forever.

The torrents carry the publisher's own piece hashes, which settles the question:

```bash
python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" --verify-only
```

This **transfers nothing and changes nothing**. Each archive is added in `upload_mode` with peer discovery disabled, rechecked against its piece hashes, then removed from the session without touching the files. Scope it with the usual filters, and write a machine-readable result with `--verify-report`:

```bash
# Verify a few archives and save the result
python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" \
  --verify-only --only "DEF CON 30,DEF CON 31" --verify-report ~/verify.json

# Verify everything, including the opt-in trees
python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" \
  --verify-only --include-mirrors --include-rainbow-tables
```

Output reports each archive, and cross-references the HTTP manifest to flag the case that matters most - a file the manifest records as verified that the piece hashes say is incomplete:

```
Verifying 2600 archive (73.55 GB, 6982 files) ...
  100.00% verified by piece hash - OK
Verifying BlueHat archive (11.20 GB, 812 files) ...
   94.31% verified by piece hash - 3 file(s) incomplete, 1 WRONGLY CATALOGUED AS GOOD

=== verification summary ===
  archives checked : 2
  verified         : 0.084 TB of 0.085 TB (98.82%)
  incomplete files : 3
  of those, recorded in the manifest as verified: 1
```

The command exits non-zero when anything is wrongly catalogued, so it can gate a script. Verification reads every byte it checks, so scope it rather than running the whole archive casually, and note that it takes the same drive-level lock as a sync - stop the sync first, or pass `--force` if you accept both processes competing for the disk.

Coverage is broad: the online inventory holds **330 torrents** spanning `cons/` (284), `podcasts/` (33), `skills/` (11), `documentaries/` and `word lists/`. Content without a torrent - notably a DEF CON year published before its torrent exists - can only be checked by size and recorded hash.

### Combined Torrent and HTTP Mode

To use the torrent-backed DEF CON content as the authoritative source while filling the rest of the archive over HTTP, run:

```bash
python infocon_scraper.py \
  --dest "/path/to/InfoCon drive" \
  --with-torrents \
  --torrent-defcon-only "30,31,32,33,34"
```

The combined workflow adds DEF CON 30–34 by default and also discovers non-mirror InfoCon torrents recursively. It immediately crawls non-DEF CON content while torrent files are checked. Once checking finishes, HTTP crawls the torrentless DEF CON remainder while libtorrent continues downloading. A single drive-level lock remains held by the parent process until all phases finish.

Torrent archives are added newest first by DEF CON number. Skip archives that are arriving separately, such as a physical DEF CON or BSides delivery, from torrent ownership so HTTP downloads them:

```bash
python infocon_scraper.py \
  --dest "/path/to/InfoCon drive" \
  --with-torrents \
  --skip-recent "DEF CON 34,BSides Las Vegas 2026"
```

The skip list is intentionally explicit so it can be changed each year when a new DEF CON torrent or physical conference delivery becomes available. It does not exclude those paths from HTTP. The standalone torrent helper accepts the equivalent `--skip` option.

If a torrent the scheduler is actively running stays at zero peers and zero download rate, combined mode pauses it and hands that archive's own folder back to HTTP after 30 minutes by default. Only torrents in the active set are timed: an archive queued behind `--torrent-max-active` reports zero peers because it has not been started yet, not because it is dead, so its quiet timer does not run until it is promoted. Tune the fallback window when needed:

```bash
python infocon_scraper.py \
  --dest "/path/to/InfoCon drive" \
  --with-torrents \
  --torrent-stalled-minutes 15
```

This fallback only applies to torrents that are actually stalled; active torrents and completed torrents remain torrent-authoritative. A standalone `fetch_defcon_torrents.py` run has no HTTP fallback and will continue waiting for its torrent set.

A fallback crawls the stalled archive's own content folder and nothing wider. `cons/2600 archive v1 - infocon.org.torrent` falls back to `cons/2600/`, not to all of `cons/`, and DEF CON archives fall back to their `media.defcon.org` year folder. Fallbacks are also skipped entirely when the ordinary sync already covers that path, deduplicated by root, and run one at a time on a shared queue, so a swarm of stalled torrents cannot multiply into concurrent crawls of the same tree.

DEF CON 34 may not have a torrent yet. Run the HTTP scraper for that year and any special folders not represented by torrents.

> The `infocon_scraper.py --fetch-torrent` option shells out to `aria2c`, which only supports BitTorrent v1. DEF CON's per-archive torrents on `media.defcon.org` are v2, so use `fetch_defcon_torrents.py` (libtorrent) for those.

## Monitoring

Run jobs detached if desired:

```bash
nohup python infocon_scraper.py --dest "/path/to/drive" >> run.out 2>&1 &
nohup python fetch_defcon_torrents.py --dest "/path/to/drive/cons/DEF CON" >> torrent.out 2>&1 &
```

Watch them live:

```bash
tail -f run.out
tail -f torrent.out
pgrep -fa 'infocon_scraper.py|fetch_defcon_torrents.py'
df -h "/path/to/drive"
```

For a detached overview with mount health, free-space/inode alerts, torrent cache size, worker CPU/memory/threads/fds/disk I/O, and recent errors:

```bash
nohup ./monitoring/monitor_infocon.sh > monitor-daemon.log 2>&1 &
tail -f infocon-monitor.out
```

The overview defaults to `/media/chiefgyk3d/infocon.org DC30`. Override it with `INFOCON_DEST`, `INFOCON_MONITOR_INTERVAL`, `INFOCON_MONITOR_OUTPUT`, or `INFOCON_TORRENT_CACHE`. Stop it with `pkill -f monitor_infocon.sh`; stopping it does not stop the scraper.

For a persistent detachable six-pane dashboard, start it once with the setup script, which creates the tmux session and starts the overview daemon above if it isn't already running:

```bash
./bin/start-dashboard.sh
./bin/tmux-infocon.sh attach -t infocon-monitor
```

The panes are:

| Pane | Script | Shows |
| --- | --- | --- |
| Scraper log | `tail -F run.out` | Raw HTTP/torrent log output |
| System | `monitor_system.sh` | Load, memory, swap, CPU breakdown, destination disk usage (`df -h`), top processes by CPU/memory |
| Network I/O | `monitor_network_io.sh` | RX/TX throughput, packet rates, errors, drops, socket counts, per-process socket/state attribution |
| Disk I/O | `monitor_disk_io.sh` | Kernel-counter read/write rates, queue depth, await, utilization, merges, since-boot totals |
| HTTP status | `monitor_http_status.sh` | Live progress bar, discovered/downloaded/skipped/error counts, `.part` staging probe, recent HTTP failures |
| BitTorrent status | `monitor_torrent_status.sh` | Active downloads always shown in full; checking/queued torrents truncated with a remaining count; state-count summary |

The overview pane just tails a snapshot file (`infocon-monitor.out`) written by the separate `monitoring/monitor_infocon.sh` daemon, so that daemon must be running for the overview pane to show live data; `bin/start-dashboard.sh` starts it automatically if it isn't already. The network and disk panes default to `eno1` and `sdc`; `bin/start-dashboard.sh` reads `INFOCON_NETWORK_INTERFACE` and `INFOCON_DISK_DEVICE` and passes them through. Running `monitoring/monitor_network_io.sh` or `monitoring/monitor_disk_io.sh` directly instead takes the interface/device as a positional argument, e.g. `./monitoring/monitor_disk_io.sh nvme0n1 10`. The torrent pane truncates checking/queued entries at `INFOCON_TORRENT_MAX_IDLE_LINES` (default 10). Detach with `Ctrl-b d`; the session and scraper continue running. Reattach with `./bin/tmux-infocon.sh attach -t infocon-monitor`, list sessions with `./bin/tmux-infocon.sh ls`, and stop only the dashboard with `./bin/tmux-infocon.sh kill-session -t infocon-monitor`.

The HTTP manifest is a SQLite database at `<destination>/.infocon_manifest.db` by default; override it with `--manifest`. An existing `.infocon_manifest.json` from an earlier version is imported once on first run, so a drive keeps every hash it has already recorded. Logs can be placed elsewhere with `--log-file`.

## Robustness

The HTTP sync is built to survive interruptions and large runs:

- **Atomic writes.** Each file downloads to a `.part` sibling and is renamed into place only once it looks complete, so a killed or truncated transfer never leaves a file that looks whole.
- **Verification without `Content-Length`.** Neither archive host sends `Content-Length`, so curl's exit status is the primary integrity signal: a truncated response surfaces as a transfer error rather than a short body. The directory listing's published size is the second check, compared within the slack implied by its printed precision - a value shown as `648.6 KiB` is known to about 102 bytes, so a badly short file is still caught.
- **Per-file retries.** Failed or size-mismatched downloads retry with backoff (`--retries`, default 4).
- **Resume where it is possible.** Partial `.part` files resume when the host honours `Range`. Neither infocon.org nor media.defcon.org does - both answer a ranged request with the whole body - so transfers there restart if interrupted. That is detected once per host at runtime rather than assumed, and the staged file is discarded instead of retried into a permanent failure.
- **Stall detection, not wall-clock caps.** A transfer is abandoned only when it averages under `--min-speed-bytes` (default 1024) for `--stall-timeout` seconds (default 300). There is no hard per-attempt time limit by default, because the largest word lists need many hours and an aborted attempt restarts from the beginning; set `--download-timeout` if you want one anyway.
- **One engine per directory.** While a torrent is fetching an archive, the HTTP sync stands off that folder entirely. libtorrent writes files sparsely, so an in-progress file already reports its *final* size on disk - and since the hosts publish no `Content-Length`, HTTP judges completeness by size and would accept a half-written file as whole, hash it, and record it as good permanently. Standing off also stops both engines transferring the same content at once. Ownership is released as soon as the torrent completes or is handed to the HTTP fallback.
- **Sparse-file guard.** Independently of that, a local file that claims the right size but is barely allocated is treated as incomplete and replaced, catching abandoned torrents and truncation from any other cause.
- **One writer per file.** Concurrent HTTP phases can discover the same file; only one worker is ever allowed to stage a given destination path, so two transfers can never share a `.part`.
- **Bounded memory.** Download scheduling is capped (`--max-pending-downloads`, default `workers * 4`) so very large trees cannot accumulate hundreds of thousands of in-memory tasks.
- **Disk-space guard.** A download that would leave less than `--min-free-gib` (default 1) free is refused, and a genuinely full destination halts the run cleanly instead of writing corrupt files. The size comes from the directory listing, since the hosts publish none over HTTP.
- **Incremental manifest writes.** The verification manifest is a SQLite database in WAL mode, so a save writes only what changed. It was previously one JSON document rewritten in full every 200 completions, which across ~450k files meant serialising tens of megabytes thousands of times while every worker waited on the same lock. Size and modification time are recorded as soon as a download lands, before its hash is computed, so an interrupted run still keeps the fast path.
- **Cheap refreshes.** A file whose local size and the listing's published modification time both match the manifest is skipped without any network request, and the listing supplies the size for everything else, so the per-file HEAD is gone. It only ever returned nothing on these hosts, and is now used solely as a fallback for listings that omit a size.
- **Single-instance lock.** A `.infocon_scraper.lock` PID file under the destination prevents two syncs from racing on the same drive. Stale locks (dead PID) are reclaimed automatically; override with `--force`.
- **Signal handling.** `SIGINT`/`SIGTERM` finish in-flight downloads, save the manifest, checkpoint torrent fast-resume data, and release the lock. The torrent phase runs on its own thread and is stopped through the same signal, rather than being left running while shutdown waits on it.
- **Honest progress.** The reported rate and totals include bytes staged by transfers still in flight, so a run pulling several multi-gigabyte archives shows real throughput instead of `0 B/s` until the first one lands.
- **Corruption detection.** A local file whose recorded hash no longer matches is re-downloaded.
- **Authenticity, where it is available.** `--verify-only` checks local data against the publisher's piece hashes, which is the only check that proves a file matches what was published rather than merely being unchanged since it was first seen. See [Verifying Against Publisher Piece Hashes](#verifying-against-publisher-piece-hashes).
- **Untrusted listing names.** A directory entry whose name is not a single path segment - absolute, or containing a separator or `..` - is ignored instead of being joined onto the destination path.

Relevant options:

```bash
python infocon_scraper.py --dest "/path/to/drive" \
  --retries 6 --stall-timeout 600 --min-free-gib 5
```

### Known Limits

These are stated plainly because knowing where a guarantee stops is part of the guarantee.

- **Size verification is only as precise as the listing.** Neither host sends `Content-Length`, so completeness is judged against the listing's rounded size within the slack its precision implies. A value printed as `648.6 KiB` is known to about 102 bytes; one printed as `4.5 GiB` is known only to about 107 MB. A file short by less than that slack, with no prior manifest entry, would be accepted. `--verify-only` is the answer where a torrent exists.
- **A recorded hash proves continuity, not authenticity.** If a file was already damaged the first time it was catalogued, its hash was taken from the damaged copy, and `--verify-all` will keep confirming it. Only piece-hash verification can tell the difference.
- **Interrupted transfers restart.** Neither host honours `Range`, so a partial HTTP download cannot be resumed and begins again. This is detected once per host at runtime rather than assumed.
- **Upstream renames are additive.** When a conference is renamed or reorganised upstream, the sync fetches it under the new path and leaves the old copy in place. Nothing is deleted automatically, so a long-lived drive accumulates superseded directories that must be reviewed by hand.
- **Content without a torrent cannot be piece-verified.** Most of the archive is covered, but a DEF CON year published before its torrent exists is checkable only by size and recorded hash.
- **Case-variant paths cannot be reproduced everywhere.** NTFS as written from Linux stores names case-sensitively; Windows, exFAT and macOS do not. The published archive contains no case collisions, so this only bites a drive that has accumulated superseded directories from earlier syncs.

## How the Two Engines Cooperate

The HTTP crawler and the BitTorrent engine can both reach the same destination paths, so the rules between them are worth stating plainly.

**Torrents own their content while they run.** When a torrent is added, it claims the directory it writes into, and the HTTP sync skips everything underneath. This is not merely an optimisation. libtorrent stores files sparsely, so an in-progress file already reports its *final* size on disk while containing holes - and because the hosts publish no `Content-Length`, HTTP judges completeness by size. Without the claim, HTTP would accept a half-written file as whole, hash it, and record it as verified permanently. Standing off also stops the two engines transferring the same bytes at once.

**Ownership is released on completion or hand-off.** A torrent that finishes releases its directory, its data already verified piece by piece, so HTTP may then inspect it. A torrent that stalls also releases it, so the HTTP fallback can take over.

**A stalled torrent is paused, never discarded.** Its files stay exactly where they are. The fallback then crawls that archive's own folder and fills gaps: complete files are kept and catalogued, missing files are fetched, wrong-sized files are re-fetched in full. Nothing is trashed and nothing complete is downloaded twice.

**Only actively scheduled torrents can stall.** An archive queued behind `--torrent-max-active` reports zero peers because it has not been started, not because it is dead. Timing those would hand the entire queue to HTTP within `--torrent-stalled-minutes` of checking finishing.

**A fallback crawls the archive's own folder and nothing wider.** `cons/2600 archive v1 - infocon.org.torrent` falls back to `cons/2600/`, never to all of `cons/`. Fallbacks are deduplicated by root, skipped when the ordinary sync already covers the path, and run one at a time on a shared queue.

**A sparse file is treated as incomplete whatever its size says.** Independently of ownership, a local file that claims the right size but is barely allocated is discarded and re-fetched. That catches abandoned torrents from earlier runs, which no live registry could know about.

## Safety Notes

- The tools do not delete remote or local archive content automatically.
- Keep sufficient free space for the largest archive you intend to fill.
- Stop the download processes before unmounting or resizing a destination disk.
- Validate that an NTFS destination is mounted read-write before starting.
- Respect the archive host's terms, bandwidth limits, and applicable law.