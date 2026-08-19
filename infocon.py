#!/usr/bin/env python3
"""Single friendly entrypoint for basic, repeat, and advanced workflows."""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = str(ROOT / ".venv/bin/python")


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
    return default if not value else value in {"y", "yes"}


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


def build_command(env: dict[str, str], interactive: bool) -> list[str]:
    destination = env.get("INFOCON_DEST", "/media/chiefgyk3d/infocon.org DC30")
    if interactive:
        destination = ask("Destination drive", destination)

    if interactive:
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
        explicit_scope = scope == 3
    else:
        defcon_only = env.get("INFOCON_TORRENT_DEFCON_ONLY", "30,31,32,33,34")
        explicit_scope = "INFOCON_TORRENT_DEFCON_ONLY" in env

    incoming_default = env.get("INFOCON_SKIP_RECENT", "DEF CON 34,BSides Las Vegas 2026")
    leave_for_http = yes_no("Leave recent physical deliveries for HTTP instead of torrents?", True) if interactive else bool(incoming_default)
    skip_recent = incoming_default if leave_for_http else ""
    if interactive and leave_for_http and "INFOCON_SKIP_RECENT" not in env:
        skip_recent = ask("Names to leave for HTTP", incoming_default)

    configured_rainbow = env.get("INFOCON_INCLUDE_RAINBOW_TABLES")
    include_rainbow = configured_rainbow.lower() in {"1", "true", "yes", "y"} if configured_rainbow else (yes_no("Include Rainbow Tables? They are separate DDV drives and multi-terabyte.", False) if interactive else False)
    workers = int(env.get("INFOCON_WORKERS", "8"))
    if interactive:
        worker_choice = menu("HTTP download workers", ["4 (gentle)", "8 (recommended)", "16 (aggressive)", "Enter a number"], 2)
        workers = number("HTTP workers", workers, 1) if worker_choice == 4 else {1: 4, 2: 8, 3: 16}[worker_choice]
    pending = int(env.get("INFOCON_MAX_PENDING_DOWNLOADS", str(workers * 4)))
    if interactive and "INFOCON_MAX_PENDING_DOWNLOADS" not in env:
        pending = number("Maximum queued HTTP downloads", workers * 4, workers)
    discovery_workers = int(env.get("INFOCON_TORRENT_DISCOVERY_WORKERS", "8"))
    stalled_minutes = int(env.get("INFOCON_TORRENT_STALLED_MINUTES", "30"))
    if interactive:
        if "INFOCON_TORRENT_DISCOVERY_WORKERS" not in env:
            discovery_workers = number("Recursive torrent discovery workers", discovery_workers, 1)
        if "INFOCON_TORRENT_STALLED_MINUTES" not in env:
            stalled_minutes = number("Minutes before a dead torrent falls back to HTTP", stalled_minutes, 1)

    command = [PYTHON, str(ROOT / "infocon_scraper.py"), "--dest", destination, "--with-torrents"]
    if skip_recent:
        command += ["--skip-recent", skip_recent]
    if defcon_only or explicit_scope:
        command += ["--torrent-defcon-only", defcon_only]
    command += ["--torrent-discovery-workers", str(discovery_workers),
                "--torrent-stalled-minutes", str(stalled_minutes),
                "--max-pending-downloads", str(pending)]
    if include_rainbow:
        command.append("--torrent-include-rainbow-tables")
    return command


def main() -> int:
    parser = argparse.ArgumentParser(
        description="InfoCon Recovery: guided archive builder with advanced CLI escape hatch"
    )
    parser.add_argument("--repeat", action="store_true", help="Run .env settings with one confirmation")
    parser.add_argument("--config", default=str(ROOT / ".env"), help="Settings file used by --repeat")
    parser.add_argument("--advanced", action="store_true", help="Pass remaining arguments directly to infocon_scraper.py")
    args, remaining = parser.parse_known_args()

    if args.advanced:
        return subprocess.call([PYTHON, str(ROOT / "infocon_scraper.py"), *remaining], cwd=ROOT)
    if remaining:
        parser.error("use --advanced before scraper options, or run without options for the guided wizard")
    env = {**load_env(Path(args.config)), **os.environ}
    command = build_command(env, interactive=not args.repeat)
    if not args.repeat:
        print("\nInfoCon Archive Builder")
        print("This wizard builds or refreshes the current online archive.")
    print("\nConfiguration:")
    print(" ".join(shlex.quote(part) for part in command))
    if not yes_no("Start this refresh now?", not args.repeat):
        print("Cancelled.")
        return 0
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
