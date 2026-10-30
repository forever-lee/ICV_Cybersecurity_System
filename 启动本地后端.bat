@echo off
cd /d "%~dp0"
set "PYTHON=%USERPROFILE%\Desktop\testS32K\TM\TM\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
"%PYTHON%" run_cloud.py
pause
