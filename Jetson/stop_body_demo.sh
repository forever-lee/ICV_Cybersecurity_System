#!/usr/bin/env bash

set -u

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${PROJECT_ROOT}/.body_demo_runtime"
CLOUD_PID_FILE="${RUNTIME_DIR}/cloud.pid"
BOARD_PID_FILE="${RUNTIME_DIR}/board.pid"

stop_managed_process() {
    local display_name="$1"
    local pid_file="$2"
    local script_name="$3"
    local process_id

    if [[ ! -f "$pid_file" ]]; then
        echo "$display_name is not recorded as running."
        return
    fi

    process_id="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ ! "$process_id" =~ ^[0-9]+$ || ! -r "/proc/${process_id}/cmdline" ]]; then
        echo "$display_name is already stopped."
        rm -f "$pid_file"
        return
    fi

    if ! tr '\0' ' ' < "/proc/${process_id}/cmdline" | grep -Fq "$script_name"; then
        echo "PID $process_id belongs to another program; it was not stopped."
        rm -f "$pid_file"
        return
    fi

    kill "$process_id" 2>/dev/null || true
    for _ in $(seq 1 20); do
        if ! kill -0 "$process_id" 2>/dev/null; then
            break
        fi
        sleep 0.25
    done

    if kill -0 "$process_id" 2>/dev/null; then
        kill -KILL "$process_id" 2>/dev/null || true
    fi
    rm -f "$pid_file"
    echo "Stopped $display_name."
}

stop_managed_process "body serial bridge" "$BOARD_PID_FILE" "Body_Module_Board.py"
stop_managed_process "cloud service" "$CLOUD_PID_FILE" "run_cloud.py"
echo "Body-domain demo stopped."
