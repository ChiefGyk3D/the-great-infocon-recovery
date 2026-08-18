#!/usr/bin/env bash
set -u

INTERVAL=${1:-10}
while :; do
    clear
    printf 'InfoCon system  %s  refresh=%ss\n\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$INTERVAL"
    printf 'Load: '; uptime | sed 's/.*load average: //'
    free -h
    printf '\nCPU / scheduler snapshot:\n'
    vmstat 1 2 | tail -n 1
    printf '\nTop processes by CPU:\n'
    ps -eo pid,comm,%cpu,%mem,nlwp,rss --sort=-%cpu | head -n 8
    printf '\nTop processes by memory:\n'
    ps -eo pid,comm,%cpu,%mem,nlwp,rss --sort=-%mem | head -n 8
    sleep "$INTERVAL"
done
