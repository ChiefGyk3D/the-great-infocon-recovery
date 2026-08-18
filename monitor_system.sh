#!/usr/bin/env bash
set -u

INTERVAL=${1:-10}
while :; do
    clear
    printf 'InfoCon system  %s  refresh=%ss\n\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$INTERVAL"
    printf 'Load: '; uptime | sed 's/.*load average: //'
    free -h
    printf '\nCPU / scheduler snapshot:\n'
    vmstat 1 2 | tail -n 1 | awk '{
        printf "procs: r=%s b=%s | memory: swpd=%sK free=%sK buff=%sK cache=%sK | " \
               "swap: si=%s so=%s | io: bi=%s bo=%s | system: in=%s cs=%s | " \
               "cpu: us=%s%% sy=%s%% id=%s%% wa=%s%% st=%s%%\n", \
               $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17
    }'
    printf '\nTop processes by CPU:\n'
    ps -eo pid,comm,%cpu,%mem,nlwp,rss --sort=-%cpu | head -n 8
    printf '\nTop processes by memory:\n'
    ps -eo pid,comm,%cpu,%mem,nlwp,rss --sort=-%mem | head -n 8
    sleep "$INTERVAL"
done
