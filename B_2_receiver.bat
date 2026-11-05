@echo off
title WiFi B - Ethernet Receiver 4.100:6001
cd /d "%~dp0"
set "PYTHON=%USERPROFILE%\Desktop\testS32K\TM\TM\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
set "CLOUD_HTTP_URL=http://127.0.0.1:8001"
set "WIFI_UDP_HOST=192.168.4.100"
echo WiFi B receiver. Ethernet IP must be 192.168.4.100. Close A receiver first.
"%PYTHON%" jetson_edge\WiFi_Module.py
pause
