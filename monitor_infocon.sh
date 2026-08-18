#!/usr/bin/env bash
set -u

DEST="${INFOCON_DEST:-/media/chiefgyk3d/infocon.org DC30}"
INTERVAL=${INFOCON_MONITOR_INTERVAL:-10}
INTERFACE=${INFOCON_NETWORK_INTERFACE:-eno1}
OUTPUT=${INFOCON_MONITOR_OUTPUT:-infocon-monitor.out}

read_net_bytes() {
    awk -v iface="$INTERFACE" '$1 ~ "^" iface ":$" {print $2 + $10; found=1} END {if (!found) print 0}' /proc/net/dev
}

mount_source=$(findmnt -no SOURCE "$DEST" 2>/dev/null || true)
disk_name=$(basename "$mount_source")
disk_name=${disk_name%%[0-9]*}
if [[ -z "$disk_name" ]]; then
    disk_name=sdc
fi

read_disk_sectors() {
    awk -v disk="$disk_name" '$3 == disk {print $6, $10; found=1} END {if (!found) print 0, 0}' /proc/diskstats
}

previous_net=$(read_net_bytes)
read -r previous_read previous_write <<< "$(read_disk_sectors)"
previous_time=$(date +%s)

while :; do
    current_time=$(date +%s)
    current_net=$(read_net_bytes)
    read -r current_read current_write <<< "$(read_disk_sectors)"
    elapsed=$((current_time - previous_time))
    if (( elapsed < 1 )); then elapsed=1; fi

    net_rate=$(( (current_net - previous_net) / elapsed ))
    read_rate=$(( (current_read - previous_read) * 512 / elapsed ))
    write_rate=$(( (current_write - previous_write) * 512 / elapsed ))

    {
        printf '\033[2J\033[H'
        printf 'InfoCon detached monitor  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
        printf 'Destination: %s\n' "$DEST"
        printf 'Network %s: %s/s total RX+TX\n' "$INTERFACE" "$(numfmt --to=iec "$net_rate")"
        printf 'Disk %s: read %s/s  write %s/s\n' "$disk_name" "$(numfmt --to=iec "$read_rate")" "$(numfmt --to=iec "$write_rate")"
        df -h "$DEST" | awk 'NR == 2 {printf "Disk space: %s used / %s available (%s)\n", $3, $4, $5}'
        if [[ -f "$DEST/.infocon_scraper.lock" ]]; then
            lock_pid=$(cat "$DEST/.infocon_scraper.lock")
            printf 'Lock: PID %s\n' "$lock_pid"
            ps -o pid,etime,stat,cmd -p "$lock_pid" 2>/dev/null || printf 'Lock process is stale\n'
        else
            printf 'Lock: absent\n'
        fi
        printf '\nProcesses:\n'
        pgrep -af 'infocon_scraper.py|fetch_defcon_torrents.py' || printf 'none\n'
        printf '\nRecent scraper output:\n'
        tail -n 12 run.out 2>/dev/null || printf 'run.out not found\n'
    } > "$OUTPUT"

    previous_net=$current_net
    previous_read=$current_read
    previous_write=$current_write
    previous_time=$current_time
    sleep "$INTERVAL"
done
