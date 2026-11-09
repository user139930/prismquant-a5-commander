@echo off
rem PrismQuant A5 Commander - one-click launcher
rem 1) 先試本機已裝好依賴的 Python（秒開）  2) 否則建 .venv 裝依賴
setlocal
cd /d "%~dp0"

set "PY=C:/Users/Admin/.workbuddy/binaries/python/versions/3.13.12/python.exe"
if not exist "%PY%" goto :tryvenv
"%PY%" -c "import fastapi,uvicorn,httpx,pydantic_settings" >nul 2>&1
if errorlevel 1 goto :tryvenv
goto :run

:tryvenv
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 建立虛擬環境 .venv ...
  python -m venv .venv
  if errorlevel 1 goto :nopython
)
set "PY=.venv\Scripts\python.exe"
echo [2/3] 安裝依賴 ...
"%PY%" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pipfail

:run
echo.
echo   PrismQuant A5 Commander
echo   網址 http://127.0.0.1:8130
echo   關掉這個視窗就會停止服務
echo.
start "" "http://127.0.0.1:8130"
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8130
goto :eof

:nopython
echo.
echo [錯誤] 找不到 Python。請安裝 Python 3.11 或以上，安裝時勾選 "Add python.exe to PATH"。
pause
goto :eof

:pipfail
echo.
echo [錯誤] 依賴安裝失敗。請檢查網絡，或手動執行：
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
pause
goto :eof
