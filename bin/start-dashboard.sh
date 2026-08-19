#!/usr/bin/env bash
set -u

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

TMUX="./bin/tmux-infocon.sh"
SESSION=infocon-monitor
DEVICE=${INFOCON_DISK_DEVICE:-sdc}
INTERFACE=${INFOCON_NETWORK_INTERFACE:-eno1}

if "$TMUX" has-session -t "$SESSION" 2>/dev/null; then
    echo "Session '$SESSION' already exists; attach with: $TMUX attach -t $SESSION"
    exit 0
fi

# The overview pane just tails a snapshot file; it needs the producer daemon running separately.
if ! pgrep -f '[m]onitor_infocon\.sh' >/dev/null; then
    nohup ./monitoring/monitor_infocon.sh > monitor-daemon.log 2>&1 &
fi

"$TMUX" new-session -d -s "$SESSION" -n dashboard "cd '$ROOT' && exec tail -F infocon-monitor.out"
"$TMUX" split-window -h -t "$SESSION:dashboard" "cd '$ROOT' && exec ./monitoring/monitor_system.sh 10"
"$TMUX" split-window -h -t "$SESSION:dashboard" "cd '$ROOT' && exec ./monitoring/monitor_network_io.sh '$INTERFACE' 10"
"$TMUX" split-window -h -t "$SESSION:dashboard" "cd '$ROOT' && exec ./monitoring/monitor_disk_io.sh '$DEVICE' 10"
"$TMUX" split-window -h -t "$SESSION:dashboard" "cd '$ROOT' && exec ./monitoring/monitor_http_status.sh"
"$TMUX" split-window -h -t "$SESSION:dashboard" "cd '$ROOT' && exec ./monitoring/monitor_torrent_status.sh"
"$TMUX" select-layout -t "$SESSION:dashboard" tiled

echo "Started. Attach with: $TMUX attach -t $SESSION"
