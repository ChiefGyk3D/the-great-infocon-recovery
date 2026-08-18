#!/usr/bin/env bash
set -u

DEVICE=${1:-sdc}
INTERVAL=${2:-10}
STAT="/sys/block/$DEVICE/stat"

read_stats() {
    if [[ -r "$STAT" ]]; then
        awk '{print $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11}' "$STAT"
    else
        printf '0 0 0 0 0 0 0 0 0 0 0\n'
    fi
}

read -r previous_reads previous_merges previous_read_sectors previous_read_ms \
    previous_writes previous_write_merges previous_write_sectors previous_write_ms \
    previous_inflight previous_io_ms previous_weighted_ms <<< "$(read_stats)"

while :; do
    read -r current_reads current_merges current_read_sectors current_read_ms \
        current_writes current_write_merges current_write_sectors current_write_ms \
        current_inflight current_io_ms current_weighted_ms <<< "$(read_stats)"

    read_delta=$((current_reads - previous_reads))
    write_delta=$((current_writes - previous_writes))
    read_bytes=$(( (current_read_sectors - previous_read_sectors) * 512 ))
    write_bytes=$(( (current_write_sectors - previous_write_sectors) * 512 ))
    io_ms_delta=$((current_io_ms - previous_io_ms))
    weighted_ms_delta=$((current_weighted_ms - previous_weighted_ms))
    if (( read_delta + write_delta > 0 )); then
        await_ms=$((weighted_ms_delta / (read_delta + write_delta)))
    else
        await_ms=0
    fi
    read_rate=$((read_bytes / INTERVAL))
    write_rate=$((write_bytes / INTERVAL))
    util_tenths=$((io_ms_delta * 10 / INTERVAL))
    if (( util_tenths > 1000 )); then util_tenths=1000; fi

    clear
    printf 'InfoCon disk I/O  %s  device=%s  refresh=%ss\n\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$DEVICE" "$INTERVAL"
    printf 'read:   %8s/s  %8s ops\n' "$(numfmt --to=iec "$read_rate")" "$read_delta"
    printf 'write:  %8s/s  %8s ops\n' "$(numfmt --to=iec "$write_rate")" "$write_delta"
    printf 'queue:  current=%s requests-in-flight\n' "$current_inflight"
    printf 'await:  %s ms average\n' "$await_ms"
    printf 'util:   %s.%s%% busy\n' "$((util_tenths / 10))" "$((util_tenths % 10))"
    printf 'merged: read=%s  write=%s\n' "$((current_merges - previous_merges))" \
        "$((current_write_merges - previous_write_merges))"
    printf 'sectors: read=%s  write=%s\n' "$current_read_sectors" "$current_write_sectors"

    previous_reads=$current_reads
    previous_merges=$current_merges
    previous_read_sectors=$current_read_sectors
    previous_read_ms=$current_read_ms
    previous_writes=$current_writes
    previous_write_merges=$current_write_merges
    previous_write_sectors=$current_write_sectors
    previous_write_ms=$current_write_ms
    previous_inflight=$current_inflight
    previous_io_ms=$current_io_ms
    previous_weighted_ms=$current_weighted_ms
    sleep "$INTERVAL"
done
