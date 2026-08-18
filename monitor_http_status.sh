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
        printf 'HTTP progress has not emitted a status line yet.\n'
    fi

    printf '\nLatest counters:\n'
    grep -E 'Progress: [0-9]+ files completed|STATUS|Done in' "$LOG" 2>/dev/null | tail -n 6 || true

    printf '\nActive HTTP staging:\n'
    part_count=$(timeout 2s find "${INFOCON_DEST:-/media/chiefgyk3d/infocon.org DC30}" -type f -name '*.part' -print 2>/dev/null | wc -l)
    printf '.part files observed in bounded scan: %s\n' "$part_count"

    printf '\nRecent HTTP failures:\n'
    grep -Ei 'ERROR|curl: \(' "$LOG" 2>/dev/null | tail -n 6 || printf 'none\n'
    sleep "$INTERVAL"
done
