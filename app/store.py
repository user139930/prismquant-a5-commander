# -*- coding: utf-8 -*-
"""使用者的可編輯偏好（目前只有關注詞）。

為什麼要獨立一個 store：關注詞是「使用者指令」，不該寫死在 .env 裡——
proposal F-07 要求 Commander 接受使用者指令，那就得讓它在介面上改得動、而且改完下一次 run 生效。
檔案在 data/prefs.json（已 gitignore），壞掉就回退到 PQ_WATCHLIST 的預設值。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.config import PROJECT_ROOT, settings

_lock = threading.RLock()
PATH = PROJECT_ROOT / "data" / "prefs.json"
MAX_TERMS = 30
MAX_LEN = 20


def _read() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_watchlist() -> list[str]:
    with _lock:
        terms = _read().get("watchlist")
    if not isinstance(terms, list) or not terms:
        return list(settings.watchlist)
    return [str(t) for t in terms][:MAX_TERMS]


def set_watchlist(terms: list[str]) -> list[str]:
    cleaned, seen = [], set()
    for t in terms or []:
        s = str(t).strip()[:MAX_LEN]
        if not s or s in seen:
            continue
        seen.add(s)
        cleaned.append(s)
        if len(cleaned) >= MAX_TERMS:
            break
    with _lock:
        data = _read()
        data["watchlist"] = cleaned
        _write(data)
    return cleaned


def reset_watchlist() -> list[str]:
    with _lock:
        data = _read()
        data.pop("watchlist", None)
        _write(data)
    return list(settings.watchlist)
