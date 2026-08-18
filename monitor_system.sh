#!/usr/bin/env bash
set -u

INTERVAL=${1:-10}
while :; do
    read -r _ _ _ _ _ _ _ _ _ _ _ _ us sy id wa st <<< "$(vmstat 1 2 | tail -n 1)"
    clear
    printf 'InfoCon system  %s  refresh=%ss\n\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$INTERVAL"
    printf 'Load: %s\n' "$(uptime | sed 's/.*load average: //')"
    free -h | awk '/^Mem:/ {printf "Memory: %s used / %s available\n", $3, $7}
                   /^Swap:/ {printf "Swap:   %s used / %s available\n", $3, $4}'
    printf 'CPU: us=%s%% sy=%s%% id=%s%% wa=%s%% st=%s%%\n' "$us" "$sy" "$id" "$wa" "$st"
    printf '\nTop processes (CPU + MEM together):\n'
    ps -eo pid,comm,%cpu,%mem,nlwp,rss --sort=-%cpu | head -n 9
    sleep "$INTERVAL"
done

