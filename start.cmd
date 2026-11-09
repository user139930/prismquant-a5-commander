@echo off
rem PrismQuant A5 Commander - one-click launcher
rem 1) 先試本機已裝好依賴的 Python（秒開）
rem 2) 否則用 python / py -3 建 .venv 裝依賴
rem 沒有 .env 也能跑：會用規則引擎 + 內建示例資料，頁面會標明。
setlocal
cd /d "%~dp0"

set "PY=C:/Users/Admin/.workbuddy/binaries/python/versions/3.13.12/python.exe"
if not exist "%PY%" goto :setup
"%PY%" -c "import fastapi,uvicorn,httpx,pydantic_settings" >nul 2>&1
if errorlevel 1 goto :setup
goto :run

:setup
if exist ".venv\Scripts\python.exe" goto :deps
echo [1/3] 建立虛擬環境 .venv ...
python -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" goto :nopython

:deps
set "PY=.venv\Scripts\python.exe"
echo [2/3] 安裝依賴（第一次會慢一點）...
"%PY%" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pipfail

:run
echo.
echo   PrismQuant A5 Commander     http://127.0.0.1:8130
echo   關掉這個視窗就會停止服務
echo.
start "" "http://127.0.0.1:8130"
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8130
goto :eof

:nopython
echo.
echo [錯誤] 找不到 Python。
echo   請安裝 Python 3.11 或以上：https://www.python.org/downloads/
echo   安裝時記得勾選 "Add python.exe to PATH"，裝完再雙擊一次。
pause
goto :eof

:pipfail
echo.
echo [錯誤] 依賴安裝失敗（通常是網絡）。
echo   手動執行：.venv\Scripts\python.exe -m pip install -r requirements.txt
pause
goto :eof
