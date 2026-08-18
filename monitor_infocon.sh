#!/usr/bin/env bash
set -u

DEST="${INFOCON_DEST:-/media/chiefgyk3d/infocon.org DC30}"
INTERVAL=${INFOCON_MONITOR_INTERVAL:-10}
INTERFACE=${INFOCON_NETWORK_INTERFACE:-eno1}
OUTPUT=${INFOCON_MONITOR_OUTPUT:-infocon-monitor.out}
TORRENT_CACHE=${INFOCON_TORRENT_CACHE:-$HOME/.cache/infocon-scraper/torrents}

read_net_bytes() {
    awk -v iface="$INTERFACE" '$1 ~ "^" iface ":$" {print $2, $10; found=1} END {if (!found) print 0, 0}' /proc/net/dev
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

worker_pids() {
    ps -eo pid=,args= | awk '$2 ~ /python/ && $0 ~ /(^|[[:space:]\/])(infocon_scraper|fetch_defcon_torrents)\.py([[:space:]]|$)/ {print $1}'
}

size_for_path() {
    du -sh "$1" 2>/dev/null | awk '{print $1}'
}

read -r previous_rx previous_tx <<< "$(read_net_bytes)"
read -r previous_read previous_write <<< "$(read_disk_sectors)"
previous_time=$(date +%s)

while :; do
    current_time=$(date +%s)
    read -r current_rx current_tx <<< "$(read_net_bytes)"
    read -r current_read current_write <<< "$(read_disk_sectors)"
    elapsed=$((current_time - previous_time))
    if (( elapsed < 1 )); then elapsed=1; fi

    rx_rate=$(( (current_rx - previous_rx) / elapsed ))
    tx_rate=$(( (current_tx - previous_tx) / elapsed ))
    read_rate=$(( (current_read - previous_read) * 512 / elapsed ))
    write_rate=$(( (current_write - previous_write) * 512 / elapsed ))

    {
        printf '\033[2J\033[H'
        printf 'InfoCon monitor  %s  refresh=%ss\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$INTERVAL"
        printf 'Destination: %s\n' "$DEST"
        printf 'Mount: %s\n' "$(findmnt -no SOURCE,FSTYPE,OPTIONS "$DEST" 2>/dev/null || printf 'NOT-MOUNTED')"
        if [[ -r "$DEST" && -w "$DEST" ]]; then
            printf 'Mount access: readable+writable\n'
        else
            printf 'Mount access: !!! NOT readable/writable !!!\n'
        fi
        space_available_bytes=$(df -P -B1 "$DEST" 2>/dev/null | awk 'NR == 2 {print $4}')
        inode_available_percent=$(df -P -i "$DEST" 2>/dev/null | awk 'NR == 2 {gsub("%", "", $5); print 100 - $5}')
        df -P -h "$DEST" | awk 'NR == 2 {printf "Space: %s used / %s available (%s)\n", $3, $4, $5}'
        df -P -i -h "$DEST" | awk 'NR == 2 {printf "Inodes: %s used / %s available (%s)\n", $3, $4, $5}'
        if [[ -n "$space_available_bytes" && "$space_available_bytes" -lt $((100 * 1024 * 1024 * 1024)) ]]; then
            printf 'ALERT: less than 100 GiB free on destination\n'
        fi
        if [[ -n "$inode_available_percent" ]] && awk "BEGIN {exit !($inode_available_percent < 5)}"; then
            printf 'ALERT: less than 5%% inodes available\n'
        fi
        printf 'Network %s: RX %s/s  TX %s/s  total %s/s\n' "$INTERFACE" \
            "$(numfmt --to=iec "$rx_rate")" "$(numfmt --to=iec "$tx_rate")" \
            "$(numfmt --to=iec "$((rx_rate + tx_rate))")"
        printf 'Disk %s: read %s/s  write %s/s\n' "$disk_name" "$(numfmt --to=iec "$read_rate")" "$(numfmt --to=iec "$write_rate")"
        printf 'Cache: torrent metadata %s at %s\n' "$(size_for_path "$TORRENT_CACHE")" "$TORRENT_CACHE"
        part_probe=$(timeout 2s find "$DEST" -type f -name '*.part' -print -quit 2>/dev/null || true)
        if [[ -n "$part_probe" ]]; then
            printf 'HTTP staging: .part files present (bounded probe; full scan disabled)\n'
        else
            printf 'HTTP staging: no .part file found by bounded probe\n'
        fi
        printf 'Load: %s\n' "$(uptime | sed 's/.*load average: //')"
        free -h | awk '/^Mem:/ {printf "Memory: %s used / %s available\n", $3, $7}'
        printf 'Connections: '
        ss -s 2>/dev/null | tr '\n' ' ' || printf 'unavailable'
        printf '\n'
        if [[ -f "$DEST/.infocon_scraper.lock" ]]; then
            lock_pid=$(cat "$DEST/.infocon_scraper.lock")
            printf 'Lock: PID %s\n' "$lock_pid"
            ps -o pid,etime,stat,%cpu,%mem,nlwp,rss,cmd -p "$lock_pid" 2>/dev/null || printf 'Lock process is stale\n'
        else
            printf 'Lock: absent\n'
        fi

        printf '\nWorker resource use:\n'
        worker_pid_list=$(worker_pids)
        if [[ -n "$worker_pid_list" ]]; then
            while read -r pid; do
                [[ -z "$pid" ]] && continue
                fd_count=$(find "/proc/$pid/fd" -maxdepth 1 -type l 2>/dev/null | wc -l)
                ps -o pid,etime,stat,%cpu,%mem,nlwp,rss,cmd --no-headers -p "$pid"
                io_read=$(awk '/^read_bytes:/ {print $2}' "/proc/$pid/io" 2>/dev/null || echo 0)
                io_write=$(awk '/^write_bytes:/ {print $2}' "/proc/$pid/io" 2>/dev/null || echo 0)
                printf '  pid %s open-fds=%s lifetime-disk-read=%s lifetime-disk-write=%s\n' \
                    "$pid" "$fd_count" "$(numfmt --to=iec "$io_read")" "$(numfmt --to=iec "$io_write")"
            done <<< "$worker_pid_list"
        else
            printf 'none\n'
        fi

        printf '\nTorrent summary:\n'
        grep -E '^--- [0-9]+/[0-9]+ complete|Initial DEF CON torrent|All requested DEF CON' run.out 2>/dev/null | tail -n 3 || printf 'not started\n'
        printf '\nHTTP summary:\n'
        grep -E 'STATUS|Progress:|Done in|Downloaded .* files' run.out 2>/dev/null | tail -n 4 || printf 'not started\n'
        printf '\nRecent errors:\n'
        grep -Ei 'error|failed|diskfull|insufficient|exception|not mounted' run.out 2>/dev/null | tail -n 5 || printf 'none\n'
    } > "${OUTPUT}.tmp.$$"
    mv -f "${OUTPUT}.tmp.$$" "$OUTPUT"

    previous_rx=$current_rx
    previous_tx=$current_tx
    previous_read=$current_read
    previous_write=$current_write
    previous_time=$current_time
    sleep "$INTERVAL"
done
