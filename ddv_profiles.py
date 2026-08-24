"""
DEF CON Data Duplication Village (DDV) source-drive profiles.

Every year the DDV hands out a set of source drives that people queue up to
copy. All of the content is published on infocon.org, so anyone can rebuild any
of those drives from home - the hard part was never access, it was knowing
*which* parts of a 30 TB archive belong on which drive, and whether what you
picked will actually fit.

This module is that map. It records, for each DDV drive:

  - the datasets it carries,
  - how each dataset is addressed on infocon.org (so the sync can be pointed
    at exactly that subset),
  - the publisher's own stated size,
  - the size actually declared by the publisher's torrents, and
  - whether the total still fits the drive it is nominally sold for.

Sizes drift as the archive grows. Two of the six drives no longer fit their
nominal capacity, and this module reports that rather than quietly trimming:
picking a smaller drive is the operator's decision, not ours.

`measured_bytes` is a snapshot taken from the v1/v2 torrents on CATALOG_DATE.
It exists so `--ddv-list` and the pre-flight capacity check can answer before
a multi-hour crawl. The live crawl remains the authority for what is actually
transferred.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field

TB = 10 ** 12

#: Date the measured_bytes/measured_files snapshot was taken from the
#: publisher's torrents. Shown in --ddv-list so stale figures are obvious.
CATALOG_DATE = "2026-08-24"

#: Filesystem metadata overhead. A drive sold as "6 TB" is 6e12 bytes, and a
#: fresh ext4 with -m 0 gives back roughly this much less. Used only to warn
#: earlier than a raw byte comparison would.
DEFAULT_FS_OVERHEAD = 0.015


@dataclass(frozen=True)
class Selection:
    """How to point the existing sync CLIs at one dataset.

    These map onto flags that already exist rather than inventing a parallel
    targeting system, so a profile is always expressible as a plain command
    the user can read, copy, and adjust.
    """

    sources: tuple[str, ...] = ()
    only_top: tuple[str, ...] = ()
    only_cons: tuple[str, ...] = ()
    only_mirrors: tuple[str, ...] = ()
    include_rainbow_tables: bool = False
    include_mirrors: bool = False
    #: Restrict the DEF CON torrent set, e.g. ("30", "31"). Empty means all.
    defcon_only: tuple[str, ...] = ()
    #: Exact archive-relative sub-trees to crawl, e.g. ("rainbow tables/ntlm",).
    #: `only_top` can only name a whole top-level section, so a dataset that is
    #: one directory *inside* such a section - every hash table is - needs its
    #: own root or the crawl silently widens to the entire section.
    paths: tuple[str, ...] = ()
    #: Torrent-name substrings identifying this dataset's torrents.
    torrent_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dataset:
    """One addressable body of content that lives on exactly one DDV drive."""

    key: str
    label: str
    drive: str
    measured_bytes: int
    measured_files: int
    selection: Selection
    #: The publisher's own stated size, where they state one. Deliberately kept
    #: separate from measured_bytes so a divergence stays visible.
    published_bytes: int | None = None
    #: Preferred acquisition route. The rainbow-table README asks explicitly
    #: for torrents ("Use the torrents please"), and they carry web seeds.
    prefer_torrent: bool = True
    note: str = ""

    @property
    def measured_tb(self) -> float:
        return self.measured_bytes / TB


@dataclass(frozen=True)
class Drive:
    """A DDV source drive."""

    letter: str
    nominal_bytes: int
    title: str
    dataset_keys: tuple[str, ...]
    note: str = ""
    #: Datasets that are valid substitutes rather than additions, e.g. an older
    #: vx-underground snapshot that still fits an 8 TB drive.
    alternates: tuple[str, ...] = field(default_factory=tuple)

    @property
    def nominal_tb(self) -> float:
        return self.nominal_bytes / TB


# ---------------------------------------------------------------------------
# The catalog
#
# Drive letters, capacities and contents follow the DDV layout documented in
# README.md. Dataset sizes come from the publisher: the stated figures from
# infocon.org's own "## READ ME RAINBOW TABLES ##.txt" (updated 2026-06-07),
# and the measured figures from summing the non-pad files declared by each
# dataset's v1/v2 torrents.
# ---------------------------------------------------------------------------

_ARCHIVE_SOURCES = ("infocon",)

DATASETS: tuple[Dataset, ...] = (
    # --- Drive A: the InfoCon archive itself -------------------------------
    Dataset(
        key="cons",
        label="Conference archives (excl. DEF CON)",
        drive="A",
        measured_bytes=3_830_000_000_000,
        measured_files=373_748,
        selection=Selection(sources=_ARCHIVE_SOURCES, only_top=("cons",),
                            paths=("cons",)),
        note=(
            "239 conferences - the bulk of Drive A. DEF CON needs no exclusion here: "
            "infocon.org's cons/ index does not link it, even though cons/DEF CON/ is "
            "directly browsable, so a cons/ crawl never descends into it."
        ),
    ),
    Dataset(
        key="defcon",
        label="DEF CON archive",
        drive="A",
        measured_bytes=1_766_000_000_000,
        measured_files=83_879,
        selection=Selection(sources=_ARCHIVE_SOURCES, only_top=("cons",),
                            paths=("cons/DEF CON",),
                            torrent_names=("DEF CON archive",)),
        note=(
            "Available both as a browsable tree under cons/DEF CON and as "
            "'DEF CON archive v1/v2'; the torrent is preferred because it carries web "
            "seeds and verifies by piece hash. media.defcon.org hosts additional "
            "material beyond this archive - that surplus is not part of Drive A."
        ),
    ),
    Dataset(
        key="skills",
        label="Skills / making",
        drive="A",
        measured_bytes=312_000_000_000,
        measured_files=55_638,
        selection=Selection(sources=_ARCHIVE_SOURCES, only_top=("skills",),
                            paths=("skills",)),
    ),
    Dataset(
        key="wordlists",
        label="Word lists",
        drive="A",
        measured_bytes=225_000_000_000,
        measured_files=25,
        selection=Selection(sources=_ARCHIVE_SOURCES, only_top=("word lists",),
                            paths=("word lists",)),
        note="A handful of very large archives rather than many files.",
    ),
    Dataset(
        key="podcasts",
        label="Podcasts",
        drive="A",
        measured_bytes=101_000_000_000,
        measured_files=13_014,
        selection=Selection(sources=_ARCHIVE_SOURCES, only_top=("podcasts",),
                            paths=("podcasts",)),
    ),
    Dataset(
        key="documentaries",
        label="Documentaries",
        drive="A",
        measured_bytes=54_000_000_000,
        measured_files=3_185,
        selection=Selection(sources=_ARCHIVE_SOURCES, only_top=("documentaries",),
                            paths=("documentaries",)),
    ),

    # --- Drive B: LANMAN / MySQL SHA-1 / NTLM ------------------------------
    Dataset(
        key="ntlm",
        label="NTLM rainbow tables",
        drive="B",
        measured_bytes=3_957_000_000_000,
        measured_files=7_982,
        published_bytes=int(3.6 * TB),
        selection=Selection(only_top=("rainbow tables",),
                            paths=("rainbow tables/ntlm",),
                            torrent_names=("NTLM rainbow tables",),
                            include_rainbow_tables=True),
        note="Directory 'rainbow tables/ntlm'. Distinct from NTLM 9 on Drive E.",
    ),
    Dataset(
        key="mysqlsha1",
        label="MySQL SHA-1 rainbow tables",
        drive="B",
        measured_bytes=1_483_000_000_000,
        measured_files=3_108,
        published_bytes=int(1.3 * TB),
        selection=Selection(only_top=("rainbow tables",),
                            paths=("rainbow tables/mysqlsha1",),
                            torrent_names=("MySQL SHA-1 rainbow tables",),
                            include_rainbow_tables=True),
        note=(
            "The DDV drive list names this slot 'MSSQL', but the archive publishes "
            "'mysqlsha1' / 'MySQL SHA-1 rainbow tables' and no MSSQL set exists. "
            "Treated as MySQL SHA-1; flagged rather than silently resolved."
        ),
    ),
    Dataset(
        key="lanman",
        label="LANMAN rainbow tables",
        drive="B",
        measured_bytes=398_000_000_000,
        measured_files=861,
        published_bytes=int(0.4 * TB),
        selection=Selection(only_top=("rainbow tables",),
                            paths=("rainbow tables/lanman",),
                            torrent_names=("LANMAN rainbow tables",),
                            include_rainbow_tables=True),
    ),

    # --- Drive C: A5/1 (GSM) and MD5 ---------------------------------------
    Dataset(
        key="md5",
        label="MD5 rainbow tables",
        drive="C",
        measured_bytes=3_890_000_000_000,
        measured_files=7_840,
        published_bytes=int(3.5 * TB),
        selection=Selection(only_top=("rainbow tables",),
                            paths=("rainbow tables/md5",),
                            torrent_names=("MD5 rainbow tables",),
                            include_rainbow_tables=True),
    ),
    Dataset(
        key="a51",
        label="A5/1 (GSM) rainbow tables",
        drive="C",
        measured_bytes=1_676_000_000_000,
        measured_files=57,
        published_bytes=int(1.5 * TB),
        selection=Selection(only_top=("rainbow tables",),
                            paths=("rainbow tables/A51",),
                            torrent_names=("A51 rainbow tables",),
                            include_rainbow_tables=True),
        note="A5/1 is the GSM stream cipher; the DDV list's 'A5/1, GSM' is one dataset.",
    ),

    # --- Drive D: vx-underground -------------------------------------------
    Dataset(
        key="vx-underground",
        label="vx-underground - 2025 June",
        drive="D",
        measured_bytes=8_475_000_000_000,
        measured_files=485_168,
        selection=Selection(sources=("mirrors",),
                            only_mirrors=("vx underground - 2025 June",),
                            paths=("mirrors/vx underground - 2025 June",),
                            torrent_names=("vx underground - 2025 June",),
                            include_mirrors=True),
        note="Latest snapshot. Exceeds an 8 TB drive; see 'vx-underground-2024'.",
    ),
    Dataset(
        key="vx-underground-2024",
        label="vx-underground - 2024 June",
        drive="D",
        measured_bytes=6_379_000_000_000,
        measured_files=258_652,
        selection=Selection(sources=("mirrors",),
                            only_mirrors=("vx underground - 2024 June",),
                            paths=("mirrors/vx underground - 2024 June",),
                            torrent_names=("vx underground - 2024 June",),
                            include_mirrors=True),
        note="Previous snapshot, kept because it still fits an 8 TB drive.",
    ),

    # --- Drive E: NTLM 9 ----------------------------------------------------
    Dataset(
        key="ntlm9",
        label="NTLM 9 rainbow tables",
        drive="E",
        measured_bytes=6_715_000_000_000,
        measured_files=10_008,
        published_bytes=int(6.7 * TB),
        selection=Selection(only_top=("rainbow tables",),
                            paths=("rainbow tables/NTLM 9",),
                            torrent_names=("NTLM 9 rainbow tables",),
                            include_rainbow_tables=True),
        note="9-character tables, ~50% effective (rainbowcrackalack.com).",
    ),

    # --- Drive F: Net-NTLMv1 ------------------------------------------------
    Dataset(
        key="net-ntlmv1",
        label="Net-NTLMv1 rainbow tables",
        drive="F",
        measured_bytes=4_748_000_000_000,
        measured_files=4_098,
        published_bytes=int(4.3 * TB),
        selection=Selection(only_top=("rainbow tables",),
                            paths=("rainbow tables/net-ntlmv1",),
                            torrent_names=("Net-NTLMv1 rainbow tables",),
                            include_rainbow_tables=True),
        note="Compressed from ~8 TB with a 3% RAR recovery record. Courtesy of Nic Losby.",
    ),
)

DRIVES: tuple[Drive, ...] = (
    Drive("A", 6 * TB, "InfoCon.org archive",
          ("cons", "defcon", "skills", "wordlists", "podcasts", "documentaries"),
          note="What this project rebuilds by default."),
    Drive("B", 6 * TB, "LANMAN, MySQL SHA-1 and NTLM hash tables",
          ("lanman", "mysqlsha1", "ntlm")),
    Drive("C", 6 * TB, "A5/1 (GSM) and MD5 hash tables",
          ("a51", "md5")),
    Drive("D", 8 * TB, "vx-underground archive",
          ("vx-underground",), alternates=("vx-underground-2024",)),
    Drive("E", 8 * TB, "NTLM-9 hash tables",
          ("ntlm9",)),
    Drive("F", 6 * TB, "Net-NTLMv1 rainbow table",
          ("net-ntlmv1",)),
)

_BY_KEY = {d.key: d for d in DATASETS}
_BY_LETTER = {d.letter: d for d in DRIVES}


# ---------------------------------------------------------------------------
# Lookup and resolution
# ---------------------------------------------------------------------------

def dataset(key: str) -> Dataset:
    try:
        return _BY_KEY[key.strip().lower()]
    except KeyError:
        raise KeyError(
            f"unknown DDV dataset {key!r}; known: {', '.join(sorted(_BY_KEY))}"
        ) from None


def drive(letter: str) -> Drive:
    try:
        return _BY_LETTER[letter.strip().upper()]
    except KeyError:
        raise KeyError(
            f"unknown DDV drive {letter!r}; known: {', '.join(sorted(_BY_LETTER))}"
        ) from None


def drive_datasets(letter: str, include_alternates: bool = False) -> list[Dataset]:
    """Datasets carried by one drive, largest first.

    Alternates are substitutes, not additions, so they are excluded by default -
    including them would double-count the same content.
    """
    d = drive(letter)
    keys = d.dataset_keys + (d.alternates if include_alternates else ())
    return sorted((_BY_KEY[k] for k in keys), key=lambda x: -x.measured_bytes)


def resolve(drives: list[str] | None = None,
            datasets: list[str] | None = None) -> list[Dataset]:
    """Resolve a mix of drive letters and dataset keys into a dataset list.

    Order is preserved by drive letter then descending size, and duplicates are
    collapsed so `--ddv A --ddv-dataset cons` does not select `cons` twice.
    """
    chosen: list[Dataset] = []
    seen: set[str] = set()

    for letter in drives or []:
        for ds in drive_datasets(letter):
            if ds.key not in seen:
                seen.add(ds.key)
                chosen.append(ds)

    for key in datasets or []:
        ds = dataset(key)
        if ds.key not in seen:
            seen.add(ds.key)
            chosen.append(ds)

    return chosen


def merge_selections(datasets: list[Dataset]) -> Selection:
    """Combine per-dataset selections into one set of CLI filters."""
    sources: list[str] = []
    only_top: list[str] = []
    only_cons: list[str] = []
    only_mirrors: list[str] = []
    include_rt = include_mirrors = False
    defcon_only: list[str] = []
    paths: list[str] = []
    torrent_names: list[str] = []

    def extend(dst: list[str], src: tuple[str, ...]) -> None:
        for item in src:
            if item not in dst:
                dst.append(item)

    for ds in datasets:
        sel = ds.selection
        extend(sources, sel.sources)
        extend(only_top, sel.only_top)
        extend(only_cons, sel.only_cons)
        extend(only_mirrors, sel.only_mirrors)
        extend(defcon_only, sel.defcon_only)
        extend(paths, sel.paths)
        extend(torrent_names, sel.torrent_names)
        include_rt = include_rt or sel.include_rainbow_tables
        include_mirrors = include_mirrors or sel.include_mirrors

    # A nested path is already covered by its parent; keeping both would list
    # the same tree twice. cons/DEF CON is the exception - it is unlinked from
    # the cons/ index, so a cons/ crawl does NOT reach it and it must survive.
    unlinked = {"cons/DEF CON"}
    paths = [p for p in paths
             if p in unlinked or not any(
                 p != other and p.startswith(other.rstrip("/") + "/")
                 for other in paths)]

    return Selection(
        sources=tuple(sources), only_top=tuple(only_top), only_cons=tuple(only_cons),
        only_mirrors=tuple(only_mirrors), include_rainbow_tables=include_rt,
        include_mirrors=include_mirrors, defcon_only=tuple(defcon_only),
        paths=tuple(paths), torrent_names=tuple(torrent_names),
    )



# ---------------------------------------------------------------------------
# Capacity arithmetic
# ---------------------------------------------------------------------------

def total_bytes(datasets: list[Dataset]) -> int:
    return sum(ds.measured_bytes for ds in datasets)


def total_files(datasets: list[Dataset]) -> int:
    return sum(ds.measured_files for ds in datasets)


def usable_bytes(nominal: int, fs_overhead: float = DEFAULT_FS_OVERHEAD) -> int:
    """Bytes actually available after formatting a drive of `nominal` size."""
    return int(nominal * (1.0 - fs_overhead))


@dataclass(frozen=True)
class Fit:
    """Whether a set of datasets fits a given capacity."""

    required: int
    capacity: int
    fits: bool
    shortfall: int          # 0 when it fits
    utilisation: float      # required / capacity

    @property
    def percent(self) -> float:
        return self.utilisation * 100.0


def fit(datasets: list[Dataset], capacity: int,
        fs_overhead: float = DEFAULT_FS_OVERHEAD) -> Fit:
    required = total_bytes(datasets)
    avail = usable_bytes(capacity, fs_overhead)
    return Fit(
        required=required,
        capacity=avail,
        fits=required <= avail,
        shortfall=max(0, required - avail),
        utilisation=(required / avail) if avail else float("inf"),
    )


def drive_fit(letter: str, fs_overhead: float = DEFAULT_FS_OVERHEAD) -> Fit:
    d = drive(letter)
    return fit(drive_datasets(letter), d.nominal_bytes, fs_overhead)


def free_space(path: str) -> int:
    return shutil.disk_usage(path).free


def preflight(datasets: list[Dataset], dest: str) -> tuple[bool, str]:
    """Compare a plan against the free space actually available at `dest`.

    Returns (ok, human-readable message). Never raises on a missing path - the
    caller decides whether an unmeasurable destination is fatal.
    """
    required = total_bytes(datasets)
    try:
        avail = free_space(dest)
    except OSError as exc:
        return True, f"could not measure free space at {dest}: {exc}"

    if required <= avail:
        return True, (
            f"{fmt_bytes(required)} required, {fmt_bytes(avail)} free "
            f"({required / avail * 100:.0f}% of available)"
        )
    return False, (
        f"{fmt_bytes(required)} required but only {fmt_bytes(avail)} free at {dest} "
        f"- short by {fmt_bytes(required - avail)}"
    )


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def fmt_bytes(n: int | float) -> str:
    if n >= TB:
        return f"{n / TB:.2f} TB"
    if n >= 10 ** 9:
        return f"{n / 10 ** 9:.1f} GB"
    return f"{n / 10 ** 6:.0f} MB"


def format_catalog(fs_overhead: float = DEFAULT_FS_OVERHEAD) -> str:
    """The --ddv-list output: every drive, its datasets, and whether it fits."""
    out: list[str] = []
    out.append("DEF CON Data Duplication Village - source-drive profiles")
    out.append(f"Sizes measured from publisher torrents on {CATALOG_DATE}; "
               f"capacity allows {fs_overhead * 100:.1f}% filesystem overhead.")
    out.append("")

    grand = 0
    for d in DRIVES:
        members = drive_datasets(d.letter)
        f = fit(members, d.nominal_bytes, fs_overhead)
        grand += f.required
        flag = "OK  " if f.fits else "OVER"
        out.append(
            f"[{flag}] Drive {d.letter}  {d.nominal_tb:>4.0f} TB nominal   {d.title}"
        )
        out.append(
            f"         {fmt_bytes(f.required):>9} across {total_files(members):>7,} files "
            f"= {f.percent:5.1f}% of usable capacity"
        )
        if not f.fits:
            out.append(
                f"         !! exceeds this drive by {fmt_bytes(f.shortfall)} - "
                f"use a larger drive or drop a dataset"
            )
        if d.note:
            out.append(f"         {d.note}")
        for ds in members:
            pub = ""
            if ds.published_bytes:
                pub = f"  (publisher states {fmt_bytes(ds.published_bytes)})"
            out.append(
                f"           {ds.key:<20} {fmt_bytes(ds.measured_bytes):>9}"
                f"  {ds.measured_files:>7,} files{pub}"
            )
            if ds.note:
                for line in _wrap(ds.note, 66):
                    out.append(f"             {line}")
        for alt_key in d.alternates:
            alt = _BY_KEY[alt_key]
            alt_fit = fit([alt], d.nominal_bytes, fs_overhead)
            mark = "fits" if alt_fit.fits else "still over"
            out.append(
                f"           alternate: {alt.key:<12} {fmt_bytes(alt.measured_bytes):>9}"
                f"  ({mark})"
            )
        out.append("")

    out.append(f"Full DDV set: {fmt_bytes(grand)} across "
               f"{sum(total_files(drive_datasets(d.letter)) for d in DRIVES):,} files")
    out.append("")
    out.append("Select with:  --ddv A            (one or more drive letters)")
    out.append("              --ddv-dataset md5,ntlm   (individual datasets)")
    out.append("Drives B, C, E and F are hash tables - keep them off the Drive A tree.")
    return "\n".join(out)


def format_plan(datasets: list[Dataset], dest: str | None = None) -> str:
    """Short summary of a resolved selection, for logging before a run."""
    out = [f"DDV selection: {len(datasets)} dataset(s), "
           f"{fmt_bytes(total_bytes(datasets))}, {total_files(datasets):,} files"]
    for ds in datasets:
        out.append(f"  drive {ds.drive}  {ds.key:<20} {fmt_bytes(ds.measured_bytes):>9}"
                   f"  {ds.label}")
    if dest:
        ok, msg = preflight(datasets, dest)
        out.append(f"  {'space OK' if ok else 'INSUFFICIENT SPACE'}: {msg}")
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


__all__ = [
    "CATALOG_DATE", "DATASETS", "DRIVES", "Dataset", "Drive", "Fit", "Selection",
    "dataset", "drive", "drive_datasets", "drive_fit", "fit", "fmt_bytes",
    "format_catalog", "format_plan", "free_space", "merge_selections",
    "preflight", "resolve", "total_bytes", "total_files", "usable_bytes",
]
