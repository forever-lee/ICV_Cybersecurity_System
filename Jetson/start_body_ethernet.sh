#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Python virtual environment was not found: ${PYTHON_BIN}"
    echo "Create it with: python3 -m venv .venv"
    echo "Then install dependencies: .venv/bin/python -m pip install -r requirements.txt"
    exit 1
fi

cd "${PROJECT_DIR}"

INGEST_TOKEN="$(${PYTHON_BIN} -c 'import run_cloud; print(run_cloud.INGEST_TOKEN)')"
export VEHICLE_INGEST_TOKEN="${INGEST_TOKEN}"

exec "${PYTHON_BIN}" "${PROJECT_DIR}/Body_Module_Ethernet.py" \
    --host 0.0.0.0 \
    --status-port 7100 \
    --board-ip 192.168.4.31 \
    --control-port 7101 \
    --cloud http://127.0.0.1:8001 \
    --vehicle-id VHC-001
