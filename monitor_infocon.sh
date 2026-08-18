#!/usr/bin/env bash
set -u

DEST="${INFOCON_DEST:-/media/chiefgyk3d/infocon.org DC30}"
INTERVAL=${INFOCON_MONITOR_INTERVAL:-10}
OUTPUT=${INFOCON_MONITOR_OUTPUT:-infocon-monitor.out}
TORRENT_CACHE=${INFOCON_TORRENT_CACHE:-$HOME/.cache/infocon-scraper/torrents}

worker_pids() {
    ps -eo pid=,args= | awk '$2 ~ /python/ && $0 ~ /(^|[[:space:]\/])(infocon_scraper|fetch_defcon_torrents)\.py([[:space:]]|$)/ {print $1}'
}

size_for_path() {
    du -sh "$1" 2>/dev/null | awk '{print $1}'
}

while :; do
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
        printf '\n'
        df -h "$DEST" 2>/dev/null
        df -h -i "$DEST" 2>/dev/null
        printf '\n'
        if [[ -n "$space_available_bytes" && "$space_available_bytes" -lt $((100 * 1024 * 1024 * 1024)) ]]; then
            printf 'ALERT: less than 100 GiB free on destination\n'
        fi
        if [[ -n "$inode_available_percent" ]] && awk "BEGIN {exit !($inode_available_percent < 5)}"; then
            printf 'ALERT: less than 5%% inodes available\n'
        fi
        printf 'Torrent cache: %s at %s\n' "$(size_for_path "$TORRENT_CACHE")" "$TORRENT_CACHE"
        printf 'Load: %s\n' "$(uptime | sed 's/.*load average: //')"
        free -h | awk '/^Mem:/ {printf "Memory: %s used / %s available\n", $3, $7}'
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

        printf '\nRecent errors:\n'
        grep -Ei 'error|failed|diskfull|insufficient|exception|not mounted' run.out 2>/dev/null | tail -n 5 || printf 'none\n'
    } > "${OUTPUT}.tmp.$$"
    mv -f "${OUTPUT}.tmp.$$" "$OUTPUT"

    sleep "$INTERVAL"
done
