#!/usr/bin/env python3
"""Interactive launcher for a fresh build or recurring InfoCon drive refresh."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def ask(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def yes_no(label: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{label} [{suffix}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


def menu(label: str, choices: list[str], default: int = 1) -> int:
    print(f"\n{label}")
    for index, choice in enumerate(choices, 1):
        print(f"  {index}) {choice}")
    while True:
        value = input(f"Choose [{default}]: ").strip()
        if not value:
            return default
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return int(value)
        print("Please choose one of the numbered options.")


def number(label: str, default: int, minimum: int = 1) -> int:
    while True:
        value = input(f"{label} [{default}]: ").strip() or str(default)
        try:
            parsed = int(value)
        except ValueError:
            parsed = 0
        if parsed >= minimum:
            return parsed
        print(f"Enter a whole number of at least {minimum}.")


def main() -> int:
    env = load_env(ROOT / ".env")
    env = {**env, **os.environ}
    print("InfoCon Archive Builder")
    print("This wizard builds or refreshes the current online archive.")
    destination = env.get("INFOCON_DEST") or ask("Destination drive", "/media/chiefgyk3d/infocon.org DC30")

    scope = menu("Torrent scope", [
        "DC30-34 (recommended for the DC30 drive)",
        "Choose DEF CON numbers",
        "All available DEF CON numbers",
    ])
    if scope == 1:
        defcon_only = "30,31,32,33,34"
    elif scope == 2:
        defcon_only = ask("DEF CON numbers, comma-separated", "30,31,32,33,34")
    else:
        defcon_only = ""
    defcon_scope_explicit = scope == 3 or bool(env.get("INFOCON_TORRENT_DEFCON_ONLY"))

    incoming_default = env.get("INFOCON_SKIP_RECENT", "DEF CON 34,BSides Las Vegas 2026")
    leave_for_http = yes_no("Leave recent physical deliveries for HTTP instead of torrents?", True)
    skip_recent = incoming_default if leave_for_http else ""
    if leave_for_http and not env.get("INFOCON_SKIP_RECENT"):
        skip_recent = ask("Names to leave for HTTP", incoming_default)

    configured_rainbow = env.get("INFOCON_INCLUDE_RAINBOW_TABLES")
    include_rainbow = configured_rainbow.lower() in {"1", "true", "yes", "y"} if configured_rainbow else yes_no(
        "Include Rainbow Tables? They are separate DDV drives and multi-terabyte.", False
    )

    worker_default = int(env.get("INFOCON_WORKERS", "8"))
    worker_choice = menu("HTTP download workers", ["4 (gentle)", "8 (recommended)", "16 (aggressive)", "Enter a number"], 2)
    workers = number("HTTP workers", worker_default, 1) if worker_choice == 4 else {1: 4, 2: 8, 3: 16}[worker_choice]
    pending = int(env.get("INFOCON_MAX_PENDING_DOWNLOADS", str(workers * 4)))
    if not env.get("INFOCON_MAX_PENDING_DOWNLOADS"):
        pending = number("Maximum queued HTTP downloads", workers * 4, workers)
    discovery_workers = int(env.get("INFOCON_TORRENT_DISCOVERY_WORKERS", "8"))
    if not env.get("INFOCON_TORRENT_DISCOVERY_WORKERS"):
        discovery_workers = number("Recursive torrent discovery workers", 8, 1)
    stalled_minutes = int(env.get("INFOCON_TORRENT_STALLED_MINUTES", "30"))
    if not env.get("INFOCON_TORRENT_STALLED_MINUTES"):
        stalled_minutes = number("Minutes before a dead torrent falls back to HTTP", 30, 1)

    command = [
        str(ROOT / ".venv/bin/python"), str(ROOT / "infocon_scraper.py"),
        "--dest", destination,
        "--with-torrents",
        "--torrent-discovery-workers", str(discovery_workers),
        "--torrent-stalled-minutes", str(stalled_minutes),
        "--max-pending-downloads", str(pending),
    ]
    if skip_recent:
        command[command.index("--with-torrents") + 1:command.index("--with-torrents") + 1] = ["--skip-recent", skip_recent]
    if defcon_only:
        command[command.index("--with-torrents") + 1:command.index("--with-torrents") + 1] = ["--torrent-defcon-only", defcon_only]
    elif defcon_scope_explicit:
        command[command.index("--with-torrents") + 1:command.index("--with-torrents") + 1] = ["--torrent-defcon-only", ""]
    if include_rainbow:
        command.append("--torrent-include-rainbow-tables")

    print("\nThis will build or refresh the online archive into:")
    print(f"  {destination}")
    print("Rainbow Tables are separate DDV drives and are excluded." if not include_rainbow else "Rainbow Tables opt-in is ENABLED.")
    print("\nCommand:")
    print(" ".join(shlex.quote(part) for part in command))
    if input("Start now? [y/N]: ").strip().lower() not in {"y", "yes"}:
        print("Cancelled.")
        return 0
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
