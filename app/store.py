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


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 12:
        return key[:3] + "***"
    return key[:7] + "…" + key[-4:]


def get_llm() -> dict:
    """回傳介面要用的設定，永遠不帶完整 key。"""
    with _lock:
        llm = (_read().get("llm") or {})
    key = str(llm.get("api_key") or settings.llm_api_key or "")
    return {
        "has_key": bool(key),
        "key_masked": mask_key(key),
        "base_url": str(llm.get("base_url") or settings.llm_base_url),
        "model": str(llm.get("model") or settings.brain_shared_model),
        "topology": settings.brain_topology,
        "source": "ui" if llm.get("api_key") else ("env" if settings.llm_api_key else "none"),
        "enabled": bool(key) and settings.brain_topology != "rule",
    }


def set_llm(api_key: str | None = None, base_url: str | None = None,
            model: str | None = None) -> dict:
    """存到本機 prefs 並立即生效（不用重啟）。"""
    with _lock:
        data = _read()
        llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}
        if api_key is not None:
            key = api_key.strip()
            if key:
                llm["api_key"] = key
            else:
                llm.pop("api_key", None)
        if base_url is not None and base_url.strip():
            llm["base_url"] = base_url.strip().rstrip("/")
        if model is not None and model.strip():
            llm["model"] = model.strip()
        data["llm"] = llm
        _write(data)

    if llm.get("api_key"):
        settings.llm_api_key = llm["api_key"]
    elif api_key is not None:
        settings.llm_api_key = ""
    if llm.get("base_url"):
        settings.llm_base_url = llm["base_url"]
    if llm.get("model"):
        settings.brain_shared_model = llm["model"]
        settings.brain_a5_model = llm["model"]
        settings.brain_a1_model = llm["model"]
        settings.brain_a2_model = llm["model"]
    return get_llm()


def reset_watchlist() -> list[str]:
    with _lock:
        data = _read()
        data.pop("watchlist", None)
        _write(data)
    return list(settings.watchlist)
