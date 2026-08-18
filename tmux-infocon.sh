#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LIB="$ROOT/.monitor-tools/usr/lib/x86_64-linux-gnu:$ROOT/.monitor-tools/lib/x86_64-linux-gnu:$ROOT/.monitor-tools/usr/lib"
export LD_LIBRARY_PATH="$LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$ROOT/.monitor-tools/usr/bin/tmux" -L infocon-monitor "$@"
