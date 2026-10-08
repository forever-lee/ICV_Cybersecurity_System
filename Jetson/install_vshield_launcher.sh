#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DESKTOP_DIR="${HOME}/Desktop/body_1"
START_SCRIPT="${SCRIPT_DIR}/start_vshield_body.sh"
STOP_SCRIPT="${SCRIPT_DIR}/stop_vshield_body.sh"
SERVICE_FILE="/etc/systemd/system/vshield-eth0.service"

mkdir -p "$DESKTOP_DIR"
chmod +x "$START_SCRIPT" "$STOP_SCRIPT" "$0"

service_tmp="$(mktemp)"
cat > "$service_tmp" <<'EOF'
[Unit]
Description=Configure V-SHIELD body-board Ethernet link
After=network.target NetworkManager.service

[Service]
Type=oneshot
ExecStart=/sbin/ip link set eth0 up
ExecStart=/sbin/ip address replace 192.168.4.100/24 dev eth0
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

sudo install -m 0644 "$service_tmp" "$SERVICE_FILE"
rm -f "$service_tmp"
sudo systemctl daemon-reload
sudo systemctl enable --now vshield-eth0.service

cat > "${DESKTOP_DIR}/V-SHIELD启动.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=启动 V-SHIELD
Comment=启动云端、车身域适配器并打开网页
Exec=/usr/bin/env VSHIELD_DESKTOP_LAUNCH=1 /bin/bash ${START_SCRIPT}
Icon=web-browser
Terminal=false
Categories=Development;
EOF

cat > "${DESKTOP_DIR}/V-SHIELD停止.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=停止 V-SHIELD
Comment=停止云端和车身域适配器
Exec=/bin/bash ${STOP_SCRIPT}
Icon=application-exit
Terminal=false
Categories=Development;
EOF

chmod +x "${DESKTOP_DIR}/V-SHIELD启动.desktop" \
         "${DESKTOP_DIR}/V-SHIELD停止.desktop"

if command -v gio >/dev/null 2>&1; then
    gio set "${DESKTOP_DIR}/V-SHIELD启动.desktop" metadata::trusted true >/dev/null 2>&1 || true
    gio set "${DESKTOP_DIR}/V-SHIELD停止.desktop" metadata::trusted true >/dev/null 2>&1 || true
fi

echo
echo "V-SHIELD 一键启动安装完成。"
echo "eth0 已设置为 192.168.4.100/24，并将在 Jetson 开机时自动配置。"
echo "以后双击桌面上的“启动 V-SHIELD”即可。"
echo "日志目录：${PROJECT_DIR}/.body_demo_runtime"
