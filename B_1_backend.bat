@echo off
title WiFi B - Backend 8001
cd /d "%~dp0"
set "PYTHON=%USERPROFILE%\Desktop\testS32K\TM\TM\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
echo WiFi B backend. Close the A backend first. Keep this window open.
"%PYTHON%" run_cloud.py
pause
