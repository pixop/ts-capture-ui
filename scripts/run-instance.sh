#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/run-instance.sh <primary|processed> [--screen]

Examples:
  scripts/run-instance.sh primary
  scripts/run-instance.sh processed --screen
EOF
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
    usage
    exit 1
fi

INSTANCE="$1"
MODE="${2:-}"

if [[ "$INSTANCE" != "primary" && "$INSTANCE" != "processed" ]]; then
    echo "Error: instance must be 'primary' or 'processed'." >&2
    usage
    exit 1
fi

if [[ -n "$MODE" && "$MODE" != "--screen" ]]; then
    echo "Error: only optional flag supported is --screen." >&2
    usage
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT_DIR/examples/${INSTANCE}.env"
UVICORN_BIN="$ROOT_DIR/.venv/bin/uvicorn"
SESSION_NAME="ts-capture-ui-${INSTANCE}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: env file not found: $ENV_FILE" >&2
    exit 1
fi

if [[ ! -x "$UVICORN_BIN" ]]; then
    echo "Error: uvicorn not found at $UVICORN_BIN" >&2
    echo "Create/install the virtualenv first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

RUN_CMD='set -a; source "$ENV_FILE"; set +a; cd "$ROOT_DIR"; exec "$UVICORN_BIN" app.main:app --host "$TS_BIND_HOST" --port "$TS_BIND_PORT"'

if [[ "$MODE" == "--screen" ]]; then
    if ! command -v screen >/dev/null 2>&1; then
        echo "Error: screen is not installed." >&2
        exit 1
    fi

    if screen -list | awk -v s="$SESSION_NAME" '$1 ~ "\\." s "$" { found=1 } END { exit found ? 0 : 1 }'; then
        echo "Screen session already exists: $SESSION_NAME"
        echo "Attach with: screen -r $SESSION_NAME"
        exit 0
    fi

    ROOT_DIR="$ROOT_DIR" ENV_FILE="$ENV_FILE" UVICORN_BIN="$UVICORN_BIN" \
        screen -dmS "$SESSION_NAME" bash -lc "$RUN_CMD"

    echo "Started $INSTANCE in screen session: $SESSION_NAME"
    echo "Attach: screen -r $SESSION_NAME"
    echo "Stop:   screen -S $SESSION_NAME -X quit"
    exit 0
fi

ROOT_DIR="$ROOT_DIR" ENV_FILE="$ENV_FILE" UVICORN_BIN="$UVICORN_BIN" \
    bash -lc "$RUN_CMD"
