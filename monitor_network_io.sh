#!/usr/bin/env bash
set -u

INTERFACE=${1:-eno1}
INTERVAL=${2:-10}

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
