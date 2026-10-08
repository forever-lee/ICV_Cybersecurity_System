#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNTIME_DIR="${PROJECT_DIR}/.body_demo_runtime"
CLOUD_PID_FILE="${RUNTIME_DIR}/cloud.pid"
BOARD_PID_FILE="${RUNTIME_DIR}/board.pid"
CLOUD_LOG="${RUNTIME_DIR}/cloud.log"
BOARD_LOG="${RUNTIME_DIR}/board.log"
BROWSER_LOG="${RUNTIME_DIR}/browser.log"
CLOUD_URL="http://127.0.0.1:8001"

if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
    PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
elif [[ -x "${HOME}/archiconda3/bin/python3" ]]; then
    PYTHON_BIN="${HOME}/archiconda3/bin/python3"
else
    PYTHON_BIN="$(command -v python3)"
fi

notify() {
    local message="$1"
    if command -v notify-send >/dev/null 2>&1; then
        notify-send "V-SHIELD" "$message" >/dev/null 2>&1 || true
    fi
}

launch_browser() {
    local url="$1"
    local user_id
    local display_name
    local xauthority_file
    local dbus_address
    local browser=""

    user_id="$(id -u)"
    display_name="${DISPLAY:-:0}"
    xauthority_file="${XAUTHORITY:-${HOME}/.Xauthority}"
    dbus_address="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/${user_id}/bus}"

    if [[ ! -f "$xauthority_file" && -f "/run/user/${user_id}/gdm/Xauthority" ]]; then
        xauthority_file="/run/user/${user_id}/gdm/Xauthority"
    fi

    for candidate in chromium-browser chromium google-chrome firefox; do
        if command -v "$candidate" >/dev/null 2>&1; then
            browser="$(command -v "$candidate")"
            break
        fi
    done

    : > "$BROWSER_LOG"
    if [[ -n "$browser" ]]; then
        DISPLAY="$display_name" \
        XAUTHORITY="$xauthority_file" \
        DBUS_SESSION_BUS_ADDRESS="$dbus_address" \
        nohup "$browser" --new-window "$url" >"$BROWSER_LOG" 2>&1 &
        return 0
    fi

    if command -v xdg-open >/dev/null 2>&1; then
        DISPLAY="$display_name" \
        XAUTHORITY="$xauthority_file" \
        DBUS_SESSION_BUS_ADDRESS="$dbus_address" \
        nohup xdg-open "$url" >"$BROWSER_LOG" 2>&1 &
        return 0
    fi
    return 1
}

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

remember_existing_process() {
    local script_path="$1"
    local pid_file="$2"
    local process_id

    process_id="$(pgrep -f "${script_path}" | head -n 1 || true)"
    if [[ "$process_id" =~ ^[0-9]+$ ]]; then
        echo "$process_id" > "$pid_file"
        return 0
    fi
    return 1
}

terminate_managed_process() {
    local pid_file="$1"
    local script_name="$2"
    local process_id

    [[ -f "$pid_file" ]] || return 0
    process_id="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ "$process_id" =~ ^[0-9]+$ ]] &&
       [[ -r "/proc/${process_id}/cmdline" ]] &&
       tr '\0' ' ' < "/proc/${process_id}/cmdline" | grep -Fq "$script_name"; then
        kill "$process_id" 2>/dev/null || true
        for _ in $(seq 1 12); do
            kill -0 "$process_id" 2>/dev/null || break
            sleep 0.25
        done
        if kill -0 "$process_id" 2>/dev/null; then
            kill -9 "$process_id" 2>/dev/null || true
        fi
    fi
    rm -f "$pid_file"
}

cloud_is_ready() {
    "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
from urllib.request import urlopen

try:
    with urlopen("http://127.0.0.1:8001/healthz", timeout=0.5) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
}

mkdir -p "$RUNTIME_DIR"

if [[ -z "${PYTHON_BIN:-}" || ! -x "$PYTHON_BIN" ]]; then
    notify "未找到 python3，启动失败"
    exit 1
fi

if ! ip -4 addr show dev eth0 2>/dev/null | grep -Fq "192.168.4.100/24"; then
    notify "eth0 尚未配置为 192.168.4.100/24，请先运行一次安装脚本"
    exit 1
fi

# Recover a matching process even if a previous stop removed its PID file.
if ! process_is_running "$CLOUD_PID_FILE" "run_cloud.py"; then
    remember_existing_process "${PROJECT_DIR}/run_cloud.py" "$CLOUD_PID_FILE" || true
fi

# A previous desktop launch can leave uvicorn stuck in its shutdown phase:
# the PID still exists although port 8001 is already closed. Remove that stale
# process before deciding whether a new cloud service is needed.
if process_is_running "$CLOUD_PID_FILE" "run_cloud.py" && ! cloud_is_ready; then
    terminate_managed_process "$CLOUD_PID_FILE" "run_cloud.py"
fi

if ! process_is_running "$CLOUD_PID_FILE" "run_cloud.py"; then
    if cloud_is_ready; then
        :
    else
        : > "$CLOUD_LOG"
        PYTHONUTF8=1 PYTHONUNBUFFERED=1 nohup "$PYTHON_BIN" \
            "${PROJECT_DIR}/run_cloud.py" >"$CLOUD_LOG" 2>&1 &
        echo "$!" > "$CLOUD_PID_FILE"
    fi
fi

cloud_ready=0
for _ in $(seq 1 40); do
    if cloud_is_ready; then
        cloud_ready=1
        break
    fi
    sleep 0.25
done

if [[ "$cloud_ready" -ne 1 ]]; then
    notify "云端服务启动失败，请查看 cloud.log"
    exit 1
fi

ingest_token="$(cd "$PROJECT_DIR" && "$PYTHON_BIN" -c 'import run_cloud; print(run_cloud.INGEST_TOKEN)')"
dashboard_token="$(cd "$PROJECT_DIR" && "$PYTHON_BIN" -c 'import run_cloud; print(run_cloud.DASHBOARD_ACCESS_TOKEN)')"

if [[ -z "$ingest_token" ]]; then
    notify "无法读取 INGEST_TOKEN，启动失败"
    exit 1
fi

if ! process_is_running "$BOARD_PID_FILE" "Body_Module_Board.py"; then
    if remember_existing_process "${PROJECT_DIR}/Body_Module_Board.py" "$BOARD_PID_FILE"; then
        :
    else
        : > "$BOARD_LOG"
        PYTHONUTF8=1 PYTHONUNBUFFERED=1 nohup "$PYTHON_BIN" \
            "${PROJECT_DIR}/Body_Module_Board.py" \
            --mode udp \
            --host 0.0.0.0 \
            --port 7100 \
            --board-host 192.168.4.31 \
            --control-port 7101 \
            --cloud "$CLOUD_URL" \
            --vehicle-id VHC-001 \
            --token "$ingest_token" \
            >"$BOARD_LOG" 2>&1 &
        board_pid=$!
        echo "$board_pid" > "$BOARD_PID_FILE"
        sleep 0.5
        if ! kill -0 "$board_pid" 2>/dev/null; then
            notify "车身域适配器启动失败，请查看 board.log"
            exit 1
        fi
    fi
fi

if ping -I eth0 -c 1 -W 1 192.168.4.31 >/dev/null 2>&1; then
    notify "云端和车身域适配器已启动"
else
    notify "程序已启动，但开发板 192.168.4.31 暂时不可达"
fi

dashboard_url="${CLOUD_URL}/"
if [[ -n "$dashboard_token" ]]; then
    dashboard_url="${CLOUD_URL}/?access_token=${dashboard_token}"
fi

if ! launch_browser "$dashboard_url"; then
    notify "程序已启动，但未找到可用浏览器"
fi

# GNOME may terminate background processes when a .desktop launcher exits.
# Keep only the invisible desktop-launch process alive until Stop V-SHIELD
# terminates the two managed services. Terminal launches still return at once.
if [[ "${VSHIELD_DESKTOP_LAUNCH:-0}" == "1" ]]; then
    while process_is_running "$CLOUD_PID_FILE" "run_cloud.py" ||
          process_is_running "$BOARD_PID_FILE" "Body_Module_Board.py"; do
        sleep 2
    done
fi

exit 0
