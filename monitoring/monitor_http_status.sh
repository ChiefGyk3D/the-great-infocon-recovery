#!/usr/bin/env bash
set -u

LOG=${INFOCON_LOG:-run.out}
INTERVAL=${INFOCON_MONITOR_INTERVAL:-10}

while :; do
    clear
    printf 'InfoCon HTTP status  %s  refresh=%ss\n\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$INTERVAL"
    latest=$(grep -E '\[[#-]+\] [0-9]+/[0-9]+ \(' "$LOG" 2>/dev/null | tail -n 1 || true)
    if [[ -n "$latest" ]]; then
        printf '%s\n' "$latest"
    else
        printf 'HTTP transfer progress has not emitted a status line yet.\n'
    fi

    printf '\nShared inventory:\n'
    latest_inventory=$(grep -E 'Running shared infocon.org directory inventory:|Shared inventory progress:|Shared infocon.org scan complete:' "$LOG" 2>/dev/null | tail -n 1 || true)
    if [[ -n "$latest_inventory" ]]; then
        printf '%s\n' "$latest_inventory"
    else
        printf 'Shared inventory has not emitted a status line yet.\n'
    fi

    printf '\nHTTP staging:\n'
    part_probe=$(timeout 2s find "${INFOCON_DEST:-/media/chiefgyk3d/infocon.org DC30}" -type f -name '*.part' -print -quit 2>/dev/null || true)
    if [[ -n "$part_probe" ]]; then
        printf 'at least one .part file in progress (bounded probe; full scan disabled)\n'
    else
        printf 'no .part file found by bounded probe\n'
    fi

    printf '\nRecent HTTP failures:\n'
    grep -Ei 'ERROR|curl: \(' "$LOG" 2>/dev/null | tail -n 6 || printf 'none\n'
    sleep "$INTERVAL"
done
