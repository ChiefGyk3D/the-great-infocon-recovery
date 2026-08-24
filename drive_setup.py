"""
Filesystem guidance for building DDV drives.

Choosing a filesystem for one of these drives is not the usual "pick ext4 and
move on". Three things make it awkward:

  1. The Data Duplication Village ships **NTFS**, not ext4, because the drives
     have to be readable by whoever plugs them in at the village - Windows,
     macOS and Linux alike. A drive formatted for one OS is a worse copy of a
     DDV drive even if it holds identical bytes.
  2. The datasets have wildly different shapes. Drive A is 529,489 files
     averaging 12 MB; Drive F is 4,098 files averaging 1.2 GB. ext4's default
     inode ratio would waste ~93 GB on a 6 TB drive - more than the free space
     these drives have to spare.
  3. Word lists include single files over 9 GB, which rules out FAT32 outright
     and makes the 4 GB question worth answering explicitly.

This module only ever *prints* commands. Formatting destroys data and picking
the wrong block device is unrecoverable, so the decision to run anything stays
with the operator.

A note on case sensitivity, since it looks like a trap and is not: the local
mirror on an ext4/ntfs-3g Linux volume can accumulate paths differing only by
case (`44Con` vs `44CON`) as upstream renames things, and those would collide
on a case-insensitive filesystem. Sampling the live archive, every such pair
had exactly one variant still published - the archive itself is collision-free,
and the duplicates are stale local copies. So NTFS and exFAT are safe for a
*fresh* build; they are only a problem when copying an old tree forward.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field

import ddv_profiles
from ddv_profiles import Dataset, fmt_bytes

GIB = 1 << 30

#: ext4 reserves 5% of the filesystem for root by default. On a 6 TB archive
#: drive that is 300 GB withheld from a dataset that already overflows.
EXT4_RESERVE_DEFAULT_PCT = 5

#: Multiple of the measured file count to provision inodes for, leaving room
#: for archive growth and for `.part` files during transfer.
INODE_HEADROOM = 3.0

#: Never provision fewer than this many inodes, however few files a dataset
#: has today - an almost-empty inode table costs little and running out is
#: unrecoverable without a reformat.
MIN_INODES = 65536


@dataclass(frozen=True)
class Filesystem:
    key: str
    label: str
    case_sensitive: bool
    journaling: bool
    #: None means "no practical limit for this archive".
    max_file_bytes: int | None
    linux: str
    windows: str
    macos: str
    summary: str
    ddv_default: bool = False
    caveats: tuple[str, ...] = field(default_factory=tuple)


FILESYSTEMS: tuple[Filesystem, ...] = (
    Filesystem(
        key="ext4",
        label="ext4",
        case_sensitive=True,
        journaling=True,
        max_file_bytes=None,
        linux="native read/write",
        windows="needs third-party drivers (read-only in practice)",
        macos="needs third-party drivers",
        summary="Best choice for a Linux-only drive. Journaled, case-sensitive "
                "like the archive itself, and cheap to fsck after a power loss.",
        caveats=(
            "Not what the DDV ships - a drive built this way is not a drop-in "
            "replacement for a village drive.",
            "Tune the inode ratio. The default provisions one inode per 16 KiB, "
            "which on a 6 TB drive burns roughly 93 GB on inode tables you will "
            "never use.",
            "Reclaim the 5% root reserve with -m 0; on 6 TB that is ~300 GB.",
        ),
    ),
    Filesystem(
        key="ntfs",
        label="NTFS",
        case_sensitive=False,
        journaling=True,
        max_file_bytes=None,
        linux="read/write via ntfs-3g (FUSE) or the in-kernel ntfs3 driver",
        windows="native read/write",
        macos="native read-only; writing needs third-party drivers",
        summary="What the Data Duplication Village actually uses. Pick this if "
                "the drive should behave like a village drive when someone else "
                "plugs it in.",
        ddv_default=True,
        caveats=(
            "Case-insensitive on Windows. A fresh build of the current archive "
            "is fine, but copying an older mirror forward can collide "
            "(44Con vs 44CON) - reconcile before copying.",
            "ntfs-3g is FUSE and noticeably slower than ext4 on hundreds of "
            "thousands of small files; the in-kernel ntfs3 driver is faster.",
            "Reserves no root space, so the full capacity is usable.",
        ),
    ),
    Filesystem(
        key="exfat",
        label="exFAT",
        case_sensitive=False,
        journaling=False,
        max_file_bytes=None,
        linux="read/write via exfatprogs (in-kernel since 5.4)",
        windows="native read/write",
        macos="native read/write",
        summary="The most portable read/write option - the only one all three "
                "systems write natively without extra software.",
        caveats=(
            "No journal. An interrupted write can corrupt the filesystem, not "
            "just the file - a real risk across a multi-day transfer or a power "
            "cut.",
            "No permissions or ownership; everything is world-writable.",
            "Large clusters waste space on small files. The archive has "
            "hundreds of thousands of sub-megabyte captions and subtitles.",
            "Case-insensitive, with the same caveat as NTFS.",
        ),
    ),
)

_BY_KEY = {f.key: f for f in FILESYSTEMS}


def filesystem(key: str) -> Filesystem:
    try:
        return _BY_KEY[key.strip().lower()]
    except KeyError:
        raise KeyError(
            f"unknown filesystem {key!r}; known: {', '.join(sorted(_BY_KEY))}"
        ) from None


def inode_count(datasets: list[Dataset]) -> int:
    """How many inodes this drive actually needs, with room to grow.

    mkfs.ext4 defaults to one inode per 16 KiB, sized for a general-purpose
    root filesystem. These drives hold a comparatively small number of very
    large files - Drive A averages 12 MB per file, Drive F averages 1.2 GB -
    so the default provisions two orders of magnitude more inodes than needed
    and charges real capacity for them.

    Stated as a count rather than a byte ratio because `-N 1600000` says what
    it means, where `-i 3958533` does not.
    """
    files = ddv_profiles.total_files(datasets)
    if not files:
        return MIN_INODES
    needed = int(files * INODE_HEADROOM)
    # Round up to a readable figure rather than emitting a false-precision number.
    step = 100_000 if needed > 500_000 else 10_000
    rounded = ((needed + step - 1) // step) * step
    return max(MIN_INODES, rounded)


def inode_savings(datasets: list[Dataset], capacity: int) -> int:
    """Bytes reclaimed versus mkfs.ext4's default ratio, at 256 B per inode."""
    default_inodes = capacity // 16384
    return max(0, (default_inodes - inode_count(datasets)) * 256)


def recommend(cross_platform: bool = False) -> Filesystem:
    """ext4 for a Linux-only drive; NTFS when it has to behave like a DDV drive."""
    return filesystem("ntfs" if cross_platform else "ext4")


def compare_table() -> str:
    rows = [
        f"{'':<8}{'case':<13}{'journal':<9}{'Linux':<12}{'Windows':<12}{'macOS':<12}",
        "-" * 66,
    ]
    for fs in FILESYSTEMS:
        rows.append(
            f"{fs.label:<8}"
            f"{('sensitive' if fs.case_sensitive else 'insensitive'):<13}"
            f"{('yes' if fs.journaling else 'NO'):<9}"
            f"{_short(fs.linux):<12}{_short(fs.windows):<12}{_short(fs.macos):<12}"
        )
    return "\n".join(rows)


def _short(capability: str) -> str:
    lowered = capability.lower()
    if lowered.startswith("native read/write"):
        return "read/write"
    if "read-only" in lowered:
        return "read-only"
    if "third-party" in lowered:
        return "extra sw"
    return "read/write"


def format_plan(fs_key: str, device: str, label: str,
                datasets: list[Dataset] | None = None) -> str:
    """Print - never run - the commands to prepare a drive.

    `device` is the whole disk (/dev/sdX); the partition it creates is
    /dev/sdX1. Nothing here is executed: choosing the wrong block device
    destroys data irreversibly, so the operator runs these by hand.
    """
    fs = filesystem(fs_key)
    datasets = datasets or []
    part = _partition_of(device)
    quoted = shlex.quote(label)

    out: list[str] = []
    out.append(f"Preparing {device} as {fs.label}"
               + (f" for {len(datasets)} dataset(s), "
                  f"{fmt_bytes(ddv_profiles.total_bytes(datasets))}, "
                  f"{ddv_profiles.total_files(datasets):,} files"
                  if datasets else ""))
    out.append("")
    out.append("!! These commands DESTROY everything on the target device.")
    out.append("!! Nothing below is run for you. Confirm the device first:")
    out.append("")
    out.append("     lsblk -o NAME,SIZE,MODEL,SERIAL,MOUNTPOINT")
    out.append(f"     sudo umount {part} 2>/dev/null || true")
    out.append("")
    out.append("1. Partition (GPT is required above 2 TB):")
    out.append(f"     sudo parted -s {device} mklabel gpt")
    out.append(f"     sudo parted -s -a optimal {device} mkpart primary 0% 100%")
    out.append("")
    out.append("2. Create the filesystem:")

    if fs.key == "ext4":
        inodes = inode_count(datasets)
        out.append(f"     sudo mkfs.ext4 -m 0 -N {inodes} -L {quoted} {part}")
        out.append("")
        out.append(f"     -m 0   drop the {EXT4_RESERVE_DEFAULT_PCT}% root reserve "
                   "(hundreds of GB on a drive this size)")
        if datasets:
            files = ddv_profiles.total_files(datasets)
            out.append(f"     -N {inodes}  enough for this drive's {files:,} files "
                       f"at {INODE_HEADROOM:g}x headroom")
            saved = inode_savings(datasets, ddv_profiles.total_bytes(datasets))
            if saved:
                out.append(f"            (~{fmt_bytes(saved)} reclaimed versus the "
                           "mkfs default ratio)")
        else:
            out.append(f"     -N {inodes}  inode count; pass --ddv or --ddv-dataset "
                       "to size this from real data")
    elif fs.key == "ntfs":
        out.append(f"     sudo mkfs.ntfs --quick -L {quoted} {part}")
        out.append("")
        out.append("     --quick  skip the full surface zero; drop it if the disk "
                   "is untrusted")
    else:
        out.append(f"     sudo mkfs.exfat -n {quoted} {part}")

    out.append("")
    out.append("3. Mount it:")
    out.append(f"     sudo mkdir -p /mnt/{label}")
    out.append(f"     {_mount_command(fs, part, label)}")
    out.append(f"     sudo chown -R \"$USER\": /mnt/{label}"
               if fs.key == "ext4" else
               "     (ownership is set by the mount options above)")
    out.append("")
    out.append("Why this filesystem:")
    for line in _wrap(fs.summary, 72):
        out.append(f"  {line}")
    if fs.ddv_default:
        out.append("  This is what the Data Duplication Village ships.")
    for caveat in fs.caveats:
        for i, line in enumerate(_wrap(caveat, 70)):
            out.append(f"  - {line}" if i == 0 else f"    {line}")
    return "\n".join(out)


def _mount_command(fs: Filesystem, part: str, label: str) -> str:
    if fs.key == "ext4":
        return f"sudo mount {part} /mnt/{label}"
    uid = 'uid=$(id -u),gid=$(id -g)'
    if fs.key == "ntfs":
        # big_writes materially improves ntfs-3g throughput on large media
        # files; windows_names refuses names Windows could not represent, which
        # keeps the drive genuinely portable rather than portable-looking.
        return (f"sudo mount -t ntfs-3g -o {uid},big_writes,windows_names "
                f"{part} /mnt/{label}")
    return f"sudo mount -t exfat -o {uid} {part} /mnt/{label}"


def _partition_of(device: str) -> str:
    """/dev/sdb -> /dev/sdb1, /dev/nvme0n1 -> /dev/nvme0n1p1."""
    base = device.rstrip("/")
    if base[-1].isdigit():   # nvme0n1, mmcblk0
        return f"{base}p1"
    return f"{base}1"


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


def format_help(drives: list[str] | None = None,
                datasets: list[str] | None = None,
                fs_key: str | None = None,
                device: str | None = None) -> str:
    """The --ddv-format-help output."""
    selected = ddv_profiles.resolve(drives, datasets) if (drives or datasets) else []

    out = ["Preparing a drive for a DDV rebuild", ""]
    out.append("The Data Duplication Village ships its drives as NTFS, so they can be")
    out.append("read by whoever plugs them in - Windows, macOS or Linux. ext4 is the")
    out.append("better filesystem on a Linux-only machine, but a drive built that way")
    out.append("is not a drop-in replacement for a village drive. Pick deliberately:")
    out.append("")
    out.append(compare_table())
    out.append("")
    out.append("  ext4   Linux-only. Recommended when the drive stays on your machine.")
    out.append("  NTFS   Matches the DDV. Recommended when others must read it.")
    out.append("  exFAT  Most portable, but unjournaled - a power cut can cost the")
    out.append("         filesystem, not just the file in flight.")
    out.append("")

    if selected:
        total = ddv_profiles.total_bytes(selected)
        files = ddv_profiles.total_files(selected)
        out.append(f"Selection: {fmt_bytes(total)} across {files:,} files "
                   f"(average {fmt_bytes(total // max(1, files))} per file)")
        out.append("")

    if fs_key and device:
        out.append(format_plan(fs_key, device, "infocon", selected))
    elif fs_key:
        out.append(f"Pass --ddv-device /dev/sdX with --ddv-format-help {fs_key} "
                   "to print the exact commands.")
    else:
        out.append("Pass --ddv-format-help ext4|ntfs|exfat (and --ddv-device /dev/sdX)")
        out.append("to print the exact commands for one of them.")
    return "\n".join(out)


__all__ = [
    "FILESYSTEMS", "Filesystem", "compare_table", "inode_count",
    "filesystem", "format_help", "format_plan", "inode_savings", "recommend",
]
