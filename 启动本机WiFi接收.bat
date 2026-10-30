@echo off
cd /d "%~dp0"
set "PYTHON=%USERPROFILE%\Desktop\testS32K\TM\TM\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
set "CLOUD_HTTP_URL=http://127.0.0.1:8001"
set "WIFI_UDP_HOST=192.168.4.100"
"%PYTHON%" jetson_edge\WiFi_Module.py
pause
