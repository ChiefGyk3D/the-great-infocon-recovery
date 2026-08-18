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
        if [[ -n "$space_available_bytes" && "$space_available_bytes" -lt $((100 * 1024 * 1024 * 1024)) ]]; then
            printf 'ALERT: less than 100 GiB free on destination\n'
        fi
        if [[ -n "$inode_available_percent" ]] && awk "BEGIN {exit !($inode_available_percent < 5)}"; then
            printf 'ALERT: less than 5%% inodes available\n'
        fi
        printf 'Torrent cache (metadata only): %s  Load: %s\n' "$(size_for_path "$TORRENT_CACHE")" "$(uptime | sed 's/.*load average: //')"
        free -h | awk '/^Mem:/ {printf "Memory: %s used / %s available\n", $3, $7}'

        printf '\nWorker:\n'
        if [[ -f "$DEST/.infocon_scraper.lock" ]]; then
            lock_pid=$(cat "$DEST/.infocon_scraper.lock")
        else
            lock_pid=""
        fi
        worker_pid_list=$(worker_pids)
        if [[ -z "$worker_pid_list" ]]; then
            printf '  none running (lock: %s)\n' "${lock_pid:-absent}"
        else
            while read -r pid; do
                [[ -z "$pid" ]] && continue
                script_name=$(ps -o args= -p "$pid" 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i ~ /\.py$/) {print $i; exit}}')
                fd_count=$(find "/proc/$pid/fd" -maxdepth 1 -type l 2>/dev/null | wc -l)
                io_read=$(awk '/^read_bytes:/ {print $2}' "/proc/$pid/io" 2>/dev/null || echo 0)
                io_write=$(awk '/^write_bytes:/ {print $2}' "/proc/$pid/io" 2>/dev/null || echo 0)
                read -r etime stat cpu mem nlwp rss <<< "$(ps -o etime=,stat=,%cpu=,%mem=,nlwp=,rss= -p "$pid" 2>/dev/null)"
                printf '  %s (pid %s%s)  up %s  cpu %s%%  mem %s%%  threads %s  rss %s  fds %s  disk r/w %s/%s\n' \
                    "${script_name:-python}" "$pid" \
                    "$([[ "$pid" == "$lock_pid" ]] && printf ' lock-owner' || printf '')" \
                    "$etime" "$cpu" "$mem" "$nlwp" "$(numfmt --to=iec "$((rss * 1024))")" "$fd_count" \
                    "$(numfmt --to=iec "$io_read")" "$(numfmt --to=iec "$io_write")"
            done <<< "$worker_pid_list"
        fi

        printf '\nRecent errors:\n'
        grep -Ei 'error|failed|diskfull|insufficient|exception|not mounted' run.out 2>/dev/null | tail -n 2 | cut -c1-120 || printf '  none\n'
    } > "${OUTPUT}.tmp.$$"
    mv -f "${OUTPUT}.tmp.$$" "$OUTPUT"

    sleep "$INTERVAL"
done

