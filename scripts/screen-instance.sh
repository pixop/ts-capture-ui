#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/screen-instance.sh <primary|processed> <attach|stop|status>
EOF
}

if [[ $# -ne 2 ]]; then
    usage
    exit 1
fi

INSTANCE="$1"
ACTION="$2"

if [[ "$INSTANCE" != "primary" && "$INSTANCE" != "processed" ]]; then
    echo "Error: instance must be 'primary' or 'processed'." >&2
    exit 1
fi

if [[ "$ACTION" != "attach" && "$ACTION" != "stop" && "$ACTION" != "status" ]]; then
    echo "Error: action must be one of: attach, stop, status." >&2
    exit 1
fi

if ! command -v screen >/dev/null 2>&1; then
    echo "Error: screen is not installed." >&2
    exit 1
fi

SESSION_NAME="ts-capture-ui-${INSTANCE}"

session_exists() {
    screen -list | awk -v s="$SESSION_NAME" '$1 ~ "\\." s "$" { found=1 } END { exit found ? 0 : 1 }'
}

case "$ACTION" in
    status)
        if session_exists; then
            echo "running: $SESSION_NAME"
            exit 0
        fi
        echo "not running: $SESSION_NAME"
        exit 1
        ;;
    attach)
        if ! session_exists; then
            echo "Session not found: $SESSION_NAME" >&2
            exit 1
        fi
        exec screen -r "$SESSION_NAME"
        ;;
    stop)
        if ! session_exists; then
            echo "Session not found: $SESSION_NAME"
            exit 0
        fi
        screen -S "$SESSION_NAME" -X quit
        echo "Stopped screen session: $SESSION_NAME"
        ;;
esac
