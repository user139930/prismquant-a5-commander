@echo off
rem PrismQuant A5 Commander - one-click launcher
rem 這個檔案只做三件事：找 Python、確認依賴、啟動服務。
rem 不需要 .env，也不需要 API key；沒有 key 會用規則引擎，頁面會標明。
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PORT=8130"
set "URL=http://127.0.0.1:%PORT%"

echo.
echo   PrismQuant A5 Commander
echo   ========================================
echo.

rem ---------- 1) 服務已經在跑？直接開瀏覽器 ----------
netstat -ano | findstr /c:":%PORT% " | findstr /i "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo   [i] 連接埠 %PORT% 已經有服務在跑，直接開瀏覽器。
  echo       如果那不是你開的，關掉那個視窗再回來。
  start "" "%URL%"
  echo.
  pause
  goto :eof
)

rem ---------- 2) 找 Python ----------
set "PY=.venv\Scripts\python.exe"
if exist "%PY%" goto :deps

set "BOOT="
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "BOOT=python"
if not defined BOOT (
  py -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "BOOT=py -3"
)
if not defined BOOT (
  for %%V in (313 312 311) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
      set "BOOT=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
    )
  )
)
if not defined BOOT goto :nopython

echo   [1/3] 建立虛擬環境 .venv（第一次約 30 秒）...
%BOOT% -m venv .venv
if not exist "%PY%" goto :nopython

:deps
echo   [2/3] 檢查依賴...
"%PY%" -c "import fastapi,uvicorn,httpx,pydantic_settings" >nul 2>&1
if errorlevel 1 (
  echo         第一次要先安裝，約 1-2 分鐘，請不要關掉這個視窗...
  "%PY%" -m pip install -q --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto :pipfail
)

:run
echo   [3/3] 啟動服務...
echo.
echo   網址   %URL%
echo   首頁   %URL%/#brief
echo   基建   %URL%/#infra      （回答隊友五個問題的那一頁）
echo.
echo   關掉這個視窗就會停止服務。
echo.
start "" "%URL%"
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
echo.
echo   服務已停止。
pause
goto :eof

:nopython
echo.
echo   [錯誤] 找不到可用的 Python（或虛擬環境建立失敗）。
echo.
echo   1. 到 https://www.python.org/downloads/ 下載 Python 3.11 或以上
echo   2. 安裝時勾選 "Add python.exe to PATH"
echo   3. 關掉這個視窗，再雙擊一次 start.cmd
echo.
echo   如果之前建過壞掉的 .venv，先刪掉這個資料夾再重試。
echo.
pause
goto :eof

:pipfail
echo.
echo   [錯誤] 依賴安裝失敗（通常是網絡問題）。
echo   手動執行這一行再試：
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
goto :eof
