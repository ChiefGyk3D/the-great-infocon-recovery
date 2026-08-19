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


def main() -> int:
    env = load_env(ROOT / ".env")
    env = {**env, **os.environ}
    destination = env.get("INFOCON_DEST") or ask("Destination drive", "/media/chiefgyk3d/infocon.org DC30")
    skip_recent = env.get("INFOCON_SKIP_RECENT") or ask(
        "Recent physical archives to leave for HTTP", "DEF CON 34,BSides Las Vegas 2026"
    )
    defcon_only = env.get("INFOCON_TORRENT_DEFCON_ONLY") or ask(
        "DEF CON torrent scope", "30,31,32,33,34"
    )
    include_rainbow = env.get("INFOCON_INCLUDE_RAINBOW_TABLES", "false").lower() in {"1", "true", "yes", "y"}
    if "INFOCON_INCLUDE_RAINBOW_TABLES" not in env:
        include_rainbow = ask("Include separate Rainbow Tables drives?", "no").lower() in {"1", "true", "yes", "y"}
    workers = env.get("INFOCON_WORKERS") or ask("HTTP download workers", "8")
    pending = env.get("INFOCON_MAX_PENDING_DOWNLOADS") or ask("HTTP max pending downloads", str(int(workers) * 4))
    discovery_workers = env.get("INFOCON_TORRENT_DISCOVERY_WORKERS") or ask("Torrent discovery workers", "8")
    stalled_minutes = env.get("INFOCON_TORRENT_STALLED_MINUTES") or ask("Dead torrent HTTP fallback minutes", "30")

    command = [
        str(ROOT / ".venv/bin/python"), str(ROOT / "infocon_scraper.py"),
        "--dest", destination,
        "--with-torrents",
        "--skip-recent", skip_recent,
        "--torrent-defcon-only", defcon_only,
        "--torrent-discovery-workers", discovery_workers,
        "--torrent-stalled-minutes", stalled_minutes,
        "--max-pending-downloads", pending,
    ]
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
