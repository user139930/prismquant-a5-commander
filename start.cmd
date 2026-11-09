@echo off
rem PrismQuant A5 Commander - one-click launcher
rem 第一次執行會自動建立 .venv 並安裝依賴，之後直接啟動。
setlocal
cd /d "%~dp0"

set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"

if not defined PYEXE (
  echo [1/3] 建立虛擬環境 .venv ...
  python -m venv .venv
  if errorlevel 1 goto :nopython
  set "PYEXE=.venv\Scripts\python.exe"
)

echo [2/3] 檢查依賴 ...
"%PYEXE%" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pipfail

echo [3/3] 啟動 http://127.0.0.1:8130
start "" "http://127.0.0.1:8130"
"%PYEXE%" -m uvicorn app.main:app --host 127.0.0.1 --port 8130
goto :eof

:nopython
echo.
echo [錯誤] 找不到 Python。請先安裝 Python 3.11 或以上，安裝時勾選 "Add python.exe to PATH"。
pause
goto :eof

:pipfail
echo.
echo [錯誤] 依賴安裝失敗。請檢查網絡，或先執行：
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
pause
goto :eof
