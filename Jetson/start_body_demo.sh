#!/usr/bin/env bash

set -u

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
RUNTIME_DIR="${PROJECT_ROOT}/.body_demo_runtime"
CLOUD_PID_FILE="${RUNTIME_DIR}/cloud.pid"
BOARD_PID_FILE="${RUNTIME_DIR}/board.pid"
CLOUD_LOG="${RUNTIME_DIR}/cloud.log"
BOARD_LOG="${RUNTIME_DIR}/board.log"
SERIAL_PORT="${BODY_SERIAL_PORT:-/dev/ttyUSB0}"
BAUDRATE="${BODY_SERIAL_BAUDRATE:-115200}"
CLOUD_URL="http://127.0.0.1:8001"

process_is_running() {
    local pid_file="$1"
    local script_name="$2"
    local process_id

    [[ -f "$pid_file" ]] || return 1
    process_id="$(cat "$pid_file" 2>/dev/null || true)"
    [[ "$process_id" =~ ^[0-9]+$ ]] || return 1
    [[ -r "/proc/${process_id}/cmdline" ]] || return 1
    tr '\0' ' ' < "/proc/${process_id}/cmdline" | grep -Fq "$script_name"
}

port_is_open() {
    "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import socket

with socket.socket() as client:
    client.settimeout(0.3)
    raise SystemExit(0 if client.connect_ex(("127.0.0.1", 8001)) == 0 else 1)
PY
}

mkdir -p "$RUNTIME_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python virtual environment was not found: $PYTHON_BIN"
    echo "Create it with: python3 -m venv .venv"
    echo "Then install dependencies: .venv/bin/python -m pip install -r requirements.txt pyserial"
    exit 1
fi

if [[ ! -e "$SERIAL_PORT" ]]; then
    echo "Serial device was not found: $SERIAL_PORT"
    echo "Connect the CH340 cable and check with: ls -l /dev/ttyUSB*"
    exit 1
fi

if [[ ! -r "$SERIAL_PORT" || ! -w "$SERIAL_PORT" ]]; then
    echo "Current user cannot access $SERIAL_PORT"
    echo "Run: sudo usermod -aG dialout $USER"
    echo "Then log out and log in again."
    exit 1
fi

if process_is_running "$CLOUD_PID_FILE" "run_cloud.py"; then
    echo "Cloud service is already running."
else
    if port_is_open; then
        echo "Port 8001 is already used by another process."
        echo "Stop the old run_cloud.py before starting this demo."
        exit 1
    fi

    PYTHONUTF8=1 PYTHONUNBUFFERED=1 nohup "$PYTHON_BIN" "$PROJECT_ROOT/run_cloud.py" \
        >"$CLOUD_LOG" 2>&1 &
    cloud_pid=$!
    echo "$cloud_pid" > "$CLOUD_PID_FILE"

    cloud_ready=0
    for _ in $(seq 1 30); do
        if ! kill -0 "$cloud_pid" 2>/dev/null; then
            break
        fi
        if port_is_open; then
            cloud_ready=1
            break
        fi
        sleep 0.5
    done

    if [[ "$cloud_ready" -ne 1 ]]; then
        echo "Cloud service failed to start. See: $CLOUD_LOG"
        rm -f "$CLOUD_PID_FILE"
        exit 1
    fi
    echo "Cloud service started (PID $cloud_pid)."
fi

if process_is_running "$BOARD_PID_FILE" "Body_Module_Board.py"; then
    echo "Body serial bridge is already running."
else
    ingest_token="$(
        cd "$PROJECT_ROOT" &&
        "$PYTHON_BIN" -c 'import run_cloud; print(run_cloud.INGEST_TOKEN)'
    )"
    if [[ -z "$ingest_token" ]]; then
        echo "INGEST_TOKEN could not be read from run_cloud.py."
        exit 1
    fi

    VEHICLE_INGEST_TOKEN="$ingest_token" PYTHONUTF8=1 PYTHONUNBUFFERED=1 \
        nohup "$PYTHON_BIN" "$PROJECT_ROOT/Body_Module_Board.py" \
        --mode serial \
        --serial-port "$SERIAL_PORT" \
        --baudrate "$BAUDRATE" \
        --cloud "$CLOUD_URL" \
        --token "$ingest_token" \
        >"$BOARD_LOG" 2>&1 &
    board_pid=$!
    echo "$board_pid" > "$BOARD_PID_FILE"
    sleep 1

    if ! kill -0 "$board_pid" 2>/dev/null; then
        echo "Body serial bridge failed to start. See: $BOARD_LOG"
        rm -f "$BOARD_PID_FILE"
        exit 1
    fi
    echo "Body serial bridge started (PID $board_pid)."
fi

echo
echo "Body-domain demo is running."
echo "Jetson IP addresses: $(hostname -I 2>/dev/null || true)"
echo "Open: http://JETSON_IP:8001/?vehicle=VHC-001#body"
echo "Logs: $RUNTIME_DIR"
echo "Run ./stop_body_demo.sh to stop the demo."
