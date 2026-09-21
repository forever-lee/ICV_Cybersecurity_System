@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WiFi UDP Receiver - Port 6001
echo.
echo ==============================================
echo  WiFi charging UDP receiver
echo  Listening on UDP 6001 and forwarding locally
echo  Keep this window open while demonstrating.
echo ==============================================
echo.
python WiFi_Module.py
echo.
echo WiFi receiver has stopped. Press any key to close this window.
pause >nul
