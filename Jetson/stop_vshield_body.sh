#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNTIME_DIR="${PROJECT_DIR}/.body_demo_runtime"

stop_managed_process() {
    local display_name="$1"
    local pid_file="$2"
    local script_name="$3"
    local process_id

    [[ -f "$pid_file" ]] || return 0
    process_id="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ "$process_id" =~ ^[0-9]+$ ]] &&
       [[ -r "/proc/${process_id}/cmdline" ]] &&
       tr '\0' ' ' < "/proc/${process_id}/cmdline" | grep -Fq "$script_name"; then
        kill "$process_id" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$process_id" 2>/dev/null || break
            sleep 0.25
        done
    fi
    rm -f "$pid_file"
    echo "$display_name stopped"
}

stop_managed_process "Body adapter" "${RUNTIME_DIR}/board.pid" "Body_Module_Board.py"
stop_managed_process "Cloud service" "${RUNTIME_DIR}/cloud.pid" "run_cloud.py"

if command -v notify-send >/dev/null 2>&1; then
    notify-send "V-SHIELD" "云端和车身域适配器已停止" >/dev/null 2>&1 || true
fi
