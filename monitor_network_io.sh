#!/usr/bin/env bash
set -u

INTERFACE=${1:-eno1}
INTERVAL=${2:-10}
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
NETHOGS=${NETHOGS_BIN:-$ROOT/.monitor-tools/usr/sbin/nethogs}

read_net() {
    awk -v iface="$INTERFACE" '$1 ~ "^" iface ":$" {print $2, $3, $4, $5, $10, $11, $12, $13; found=1} END {if (!found) print 0, 0, 0, 0, 0, 0, 0, 0}' /proc/net/dev
}

read -r previous_rx previous_rx_packets previous_rx_errors previous_rx_drops \
    previous_tx previous_tx_packets previous_tx_errors previous_tx_drops <<< "$(read_net)"

while :; do
    clear
    printf 'InfoCon network I/O  %s  interface=%s  refresh=%ss\n\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$INTERFACE" "$INTERVAL"
    read -r current_rx current_rx_packets current_rx_errors current_rx_drops \
        current_tx current_tx_packets current_tx_errors current_tx_drops <<< "$(read_net)"

    rx_bytes=$(( (current_rx - previous_rx) / INTERVAL ))
    tx_bytes=$(( (current_tx - previous_tx) / INTERVAL ))
    rx_packets=$(( (current_rx_packets - previous_rx_packets) / INTERVAL ))
    tx_packets=$(( (current_tx_packets - previous_tx_packets) / INTERVAL ))

    printf 'receive: %8s/s  %8s packets/s\n' "$(numfmt --to=iec "$rx_bytes")" "$rx_packets"
    printf 'transmit:%8s/s  %8s packets/s\n' "$(numfmt --to=iec "$tx_bytes")" "$tx_packets"
    printf 'errors:  RX=%s  TX=%s\n' "$((current_rx_errors - previous_rx_errors))" "$((current_tx_errors - previous_tx_errors))"
    printf 'drops:   RX=%s  TX=%s\n' "$((current_rx_drops - previous_rx_drops))" "$((current_tx_drops - previous_tx_drops))"
    printf '\nPer-process network attribution:\n'
    if [[ -x "$NETHOGS" ]] && getcap "$NETHOGS" 2>/dev/null | grep -q 'cap_net_admin'; then
        printf 'nethogs capability detected; use the nethogs pane for byte rates.\n'
    else
        printf 'byte attribution unavailable: nethogs needs CAP_NET_ADMIN and CAP_NET_RAW\n'
        printf 'showing socket/state attribution and process disk-flow counters:\n'
        worker_pids=$(ps -eo pid=,args= | awk '$2 ~ /python/ && $0 ~ /(^|[[:space:]\/])(infocon_scraper|fetch_defcon_torrents)\.py([[:space:]]|$)/ {print $1}')
        while read -r pid; do
            [[ -z "$pid" ]] && continue
            command_name=$(ps -o comm= -p "$pid" 2>/dev/null)
            socket_count=$(find "/proc/$pid/fd" -maxdepth 1 -type l -lname 'socket:*' 2>/dev/null | wc -l)
            socket_states=$(ss -tpn 2>/dev/null | awk -v pid="$pid" '$0 ~ "pid=" pid "," {print $1}' | sort | uniq -c | tr '\n' ' ')
            disk_read=$(awk '/^read_bytes:/ {print $2}' "/proc/$pid/io" 2>/dev/null || echo 0)
            disk_write=$(awk '/^write_bytes:/ {print $2}' "/proc/$pid/io" 2>/dev/null || echo 0)
            printf 'pid=%s %s sockets=%s states=[%s] lifetime-disk-read=%s lifetime-disk-write=%s\n' \
                "$pid" "$command_name" "$socket_count" "$socket_states" \
                "$(numfmt --to=iec "$disk_read")" "$(numfmt --to=iec "$disk_write")"
        done <<< "$worker_pids"
    fi
    printf '\nSocket summary:\n'
    ss -s 2>/dev/null || printf 'unavailable\n'

    previous_rx=$current_rx
    previous_rx_packets=$current_rx_packets
    previous_rx_errors=$current_rx_errors
    previous_rx_drops=$current_rx_drops
    previous_tx=$current_tx
    previous_tx_packets=$current_tx_packets
    previous_tx_errors=$current_tx_errors
    previous_tx_drops=$current_tx_drops
    sleep "$INTERVAL"
done
