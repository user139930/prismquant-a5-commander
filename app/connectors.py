# -*- coding: utf-8 -*-
"""上游連接器：A1 新聞、A2 政要監控。

設計要點（回應隊友的意見）：
- A1 的契約直接對接 github.com/user139930/prismquant-a1-news 的 `/api/v1/news/events`，
  不需要改 A1 一行代碼就能接上。
- A2 有兩條路：優先讀已經做完影響分析的 `/api/v1/analyses`；若那個端點沒有，
  退回 `/api/v1/public-records` 讀原始記錄並標記 `degraded`——這樣 A2 由 RSS 換成
  爬蟲、或 schema 微調時，A5 不會整條鏈斷掉。
- 任何一個上游失敗都不 raise，回傳 ok=False 並附錯誤摘要，由指揮官決定降級方式。
"""
from __future__ import annotations

import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.config import settings

FIXTURE = Path(__file__).parent / "fixtures" / "sample_bundle.json"


def _reachable(url: str, timeout: float = 0.25) -> bool:
    """先探一下 port。

    Windows 上對「沒有監聽的 localhost port」發 SYN，有時不是立刻 refused，而是等到
    connect timeout（實測 2 秒）。A1/A2 沒開的時候，每次 run 就白等 4 秒。
    先做一次 0.25 秒的 TCP 探測，offline 的情況可以直接短路，不影響上游正常時的速度。
    """
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _get(url: str, path: str, params: dict) -> tuple[dict | None, dict]:
    started = time.time()
    if not _reachable(url):
        return None, {"ok": False, "status": "offline", "latency_ms": int((time.time() - started) * 1000),
                      "error": "port closed"}
    try:
        # trust_env=False：上游都是本機服務，不能被系統代理（HTTP_PROXY）帶走，
        # 否則在企業網絡或 Clash 開著的機器上，本機呼叫會被代理拖到 timeout。
        with httpx.Client(timeout=settings.upstream_timeout, trust_env=False) as client:
            resp = client.get(url.rstrip("/") + path, params=params)
        latency = int((time.time() - started) * 1000)
        if resp.status_code >= 400:
            return None, {"ok": False, "status": "error", "latency_ms": latency,
                          "error": "HTTP %d" % resp.status_code}
        return resp.json(), {"ok": True, "status": "connected", "latency_ms": latency, "error": None}
    except Exception as exc:
        return None, {"ok": False, "status": "offline",
                      "latency_ms": int((time.time() - started) * 1000),
                      "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}


def a1_events(limit: int | None = None) -> tuple[list[dict], dict]:
    limit = limit or settings.upstream_limit_a1
    data, meta = _get(settings.a1_api_url, "/api/v1/news/events", {"limit": limit})
    if not meta["ok"] or not isinstance(data, dict):
        return [], meta
    events = data.get("events") or []
    meta.update({"count": len(events), "sampled": bool(data.get("sampled")),
                 "agent": data.get("agent", "A1 新聞情報")})
    return events, meta


def a2_events(limit: int | None = None) -> tuple[list[dict], dict]:
    limit = limit or settings.upstream_limit_a2
    data, meta = _get(settings.a2_api_url, "/api/v1/analyses", {"limit": limit})
    if meta["ok"] and isinstance(data, dict) and data.get("events"):
        events = data["events"]
        meta.update({"count": len(events), "degraded": False, "agent": data.get("agent", "A2 政要監控")})
        return events, meta

    if meta.get("status") == "offline":
        # 連線層就掛了，第二個端點一定也掛，不要再付一次 timeout 的代價
        return [], meta

    # 退化路徑：A2 只開放了原始記錄端點
    fallback, meta2 = _get(settings.a2_api_url, "/api/v1/public-records",
                           {"limit": limit, "order": "published_desc"})
    if meta2["ok"] and isinstance(fallback, dict):
        items = fallback.get("items") or []
        mapped = [{
            "record_id": it.get("record_id"),
            "title": it.get("title", ""),
            "summary": it.get("body_text", "")[:200],
            "direction": "中性",
            "entities": [],
            "markets": [],
            "confidence": None,
            "original_url": it.get("original_url", ""),
            "published_at": it.get("published_at"),
        } for it in items]
        meta2.update({"count": len(mapped), "degraded": True,
                      "note": "A2 未提供 /api/v1/analyses，已退回原始記錄（無方向標籤）"})
        return mapped, meta2
    return [], meta


def health() -> dict:
    out = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_a1 = pool.submit(_get, settings.a1_api_url, "/health", {})
        f_a2 = pool.submit(_get, settings.a2_api_url, "/health", {})
        (a1_data, a1_meta), (a2_data, a2_meta) = f_a1.result(), f_a2.result()
    out["a1"] = {**a1_meta, "service": (a1_data or {}).get("service", "A1 新聞情報")}
    out["a2"] = {**a2_meta, "service": (a2_data or {}).get("service", "A2 政要監控")}
    out["a4"] = {"ok": bool(settings.a4_api_url), "status": "not_connected",
                 "note": "A4 風控未接入，A5 只有建議權"}
    out["a5"] = {"ok": True, "status": "connected", "service": settings.service_name}
    return out


def fetch_all(a1_limit: int | None = None, a2_limit: int | None = None):
    """並行取兩邊；離線時總延遲由最慢的一邊決定，而不是相加。"""
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(a1_events, a1_limit)
        f2 = pool.submit(a2_events, a2_limit)
        return f1.result(), f2.result()


def sample_bundle() -> dict:
    """載入內建示例資料，並把 ago_hours 換算成真實時間戳（示範畫面才不會出現陳年舊聞）。"""
    try:
        bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
    except Exception:
        return {"a1": {"events": []}, "a2": {"events": []}}

    now = datetime.now(timezone(timedelta(hours=8)))
    for group in ("a1", "a2"):
        for e in bundle.get(group, {}).get("events", []):
            hours = e.pop("ago_hours", None)
            if hours is None:
                continue
            ts = (now - timedelta(hours=float(hours))).isoformat(timespec="seconds")
            if group == "a1":
                e["published"] = ts
            else:
                e["published_at"] = ts
            e["sampled"] = True
    return bundle
