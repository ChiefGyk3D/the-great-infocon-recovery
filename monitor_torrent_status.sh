#!/usr/bin/env bash
set -u

LOG=${INFOCON_LOG:-run.out}
INTERVAL=${INFOCON_MONITOR_INTERVAL:-10}
# Active downloads must never scroll off the pane, so they're always printed
# in full; checking/queued entries are truncated with a remaining count.
MAX_IDLE_LINES=${INFOCON_TORRENT_MAX_IDLE_LINES:-10}

while :; do
    clear
    printf 'InfoCon BitTorrent status  %s  refresh=%ss\n\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$INTERVAL"
    awk -v max_idle="$MAX_IDLE_LINES" '
        /^--- [0-9]+\/[0-9]+ complete/ {
            summary=$0
            count=0
            delete torrent_lines
            next
        }
        /^DEF CON .*: +[0-9.]+%/ {
            torrent_lines[++count]=$0
        }
        END {
            if (summary == "") {
                print "No torrent status block found yet."
                exit
            }
            print summary
            print ""

            active_count = 0
            idle_count = 0
            for (i = 1; i <= count; i++) {
                line = torrent_lines[i]
                rate = line
                sub(/^.*down +/, "", rate)
                sub(/ MB\/s.*/, "", rate)
                if ((rate + 0 > 0) || (line ~ /state downloading/)) {
                    active_lines[++active_count] = line
                } else {
                    idle_lines[++idle_count] = line
                }
            }

            printf "Active downloads (%d):\n", active_count
            if (active_count == 0) print "  none currently downloading"
            for (i = 1; i <= active_count; i++) print "  " active_lines[i]

            shown_idle = (idle_count < max_idle) ? idle_count : max_idle
            if (shown_idle > 0) {
                printf "\nChecking/queued (%d of %d):\n", shown_idle, idle_count
                for (i = 1; i <= shown_idle; i++) print "  " idle_lines[i]
            }
            if (idle_count > shown_idle) {
                printf "\n... %d more checking/queued not shown\n", idle_count - shown_idle
            }
        }
    ' "$LOG" 2>/dev/null

    printf '\nState counts from latest block:\n'
    awk '
        /^--- [0-9]+\/[0-9]+ complete/ {capture=1; delete states; next}
        capture && /^DEF CON .*: +[0-9.]+%/ {
            line=$0
            sub(/^.*state /, "", line)
            states[line]++
        }
        END {
            for (state in states) printf "%6d  %s\n", states[state], state
        }
    ' "$LOG" 2>/dev/null | sort -k2
    sleep "$INTERVAL"
done
