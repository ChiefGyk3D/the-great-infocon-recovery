#!/usr/bin/env python3
"""Single friendly entrypoint for basic, repeat, and advanced workflows."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = str(ROOT / ".venv/bin/python")


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
    command = [PYTHON, str(ROOT / "infocon_interactive.py")]
    if args.repeat:
        command += ["--repeat", "--config", args.config]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
