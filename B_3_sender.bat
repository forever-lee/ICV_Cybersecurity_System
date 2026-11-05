@echo off
title WiFi B - BMS Sender WiFi 3.2
cd /d "%~dp0"
set "PYTHON=%USERPROFILE%\Desktop\testS32K\TM\TM\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
echo WiFi B sender. WiFi IP must be 192.168.3.2. Close A sender first.
"%PYTHON%" bms_wifi_sender.py
pause
