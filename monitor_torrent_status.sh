#!/usr/bin/env bash
set -u

LOG=${INFOCON_LOG:-run.out}
INTERVAL=${INFOCON_MONITOR_INTERVAL:-10}

while :; do
    clear
    printf 'InfoCon BitTorrent status  %s  refresh=%ss\n\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$INTERVAL"
    awk '
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
            printf "Tracked status lines: %d\n\n", count
            for (i=1; i<=count; i++) print torrent_lines[i]
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
