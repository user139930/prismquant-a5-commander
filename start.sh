#!/usr/bin/env bash
# PrismQuant A5 Commander - macOS / Linux launcher
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] 建立虛擬環境 .venv ..."
  python3 -m venv .venv
fi

echo "[2/3] 檢查依賴 ..."
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

echo "[3/3] 啟動 http://127.0.0.1:8130"
(sleep 1 && (command -v open >/dev/null && open http://127.0.0.1:8130 || xdg-open http://127.0.0.1:8130 2>/dev/null)) &
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8130
