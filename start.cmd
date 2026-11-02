@echo off
rem PrismQuant A5 commander agent (thin wrapper)
cd /d "%~dp0"
set "PY=C:\Users\Admin\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8130
pause
