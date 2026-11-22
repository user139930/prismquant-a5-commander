# -*- coding: utf-8 -*-
"""合規護欄：A5 對外說話前的最後一道閘。

三件事：
1. 違規措辭改寫——「必漲」「穩賺」「內幕」這類表述**改寫成中性說法**而不是整句刪掉：
   資訊保留，承諾去掉。詞表與改寫規則在 app/compliance.py（同一份檔案也給 Winter 用）。
2. 證據可追溯——結構化欄位（傳導鏈、分歧）與正文裡引用的每個 ID 都必須真實存在，
   對不上就標 broken / broken_evidence，相關結論標「不可採用」。
3. 執行邊界——A4 未接入時，A5 只有建議權；正文不出現明確倉位，輸出帶 advisory_only。
"""
from __future__ import annotations

import copy
import re

from app.compliance import BANNED_PHRASES, DISCLAIMER, Compliance
from app.config import settings

# 一份實例走天下：詞表 = compliance 內建 + .env 追加的
_guard = Compliance(extra_banned=[p for p in settings.banned_phrases if p not in BANNED_PHRASES])


def scan(text: str) -> list[str]:
    """只回報命中了哪些詞，不改寫。給需要「先看有沒有問題」的場合用。"""
    low = (text or "").lower()
    return [p for p in _guard.banned if p and p.lower() in low]


def _clean(text: str, hits: list[dict], path: str) -> str:
    """改寫而不是刪句：保留資訊，去掉承諾。"""
    verdict = _guard.apply(text, check_position=False)
    for b in verdict["blocked"]:
        hits.append({"path": path, "phrase": b["phrase"], "replacement": b["replacement"]})
    return verdict["text"]


def sanitize(payload: dict) -> tuple[dict, list[dict]]:
    hits: list[dict] = []

    def walk(node, path):
        if isinstance(node, dict):
            return {k: walk(v, "%s.%s" % (path, k)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, "%s[%d]" % (path, i)) for i, v in enumerate(node)]
        if isinstance(node, str):
            return _clean(node, hits, path)
        return node

    return walk(copy.deepcopy(payload), "payload"), hits


def verify_evidence(payload: dict) -> list[str]:
    ids = {e.get("id") for e in payload.get("evidence", []) if isinstance(e, dict)}
    missing = []
    for row in payload.get("transmission", []) + payload.get("conflicts", []):
        for key in ("evidence", "a1_evidence", "a2_evidence"):
            for ref in row.get(key, []) or []:
                if ref and ref not in ids and ref not in missing:
                    missing.append(ref)
    return missing


def apply(payload: dict) -> dict:
    """回傳附帶 guard 結果的 payload（原 payload 不被修改）。"""
    clean, hits = sanitize(payload)
    missing = verify_evidence(clean)

    a4_connected = bool(settings.a4_api_url)
    _guard.a4_connected = a4_connected          # A4 接上後，倉位限制自動解除
    gate = "risk_checked" if (a4_connected and settings.a4_gate == "risk_checked") else "advisory_only"

    # 倉位只檢查正文，不掃全部欄位（結構化欄位可能有假設性數字）
    if not a4_connected:
        for key in ("headline", "brief", "brief_text"):
            if isinstance(clean.get(key), str):
                v = _guard.apply(clean[key], check_position=True)
                if v["flagged"] and "倉位" in "".join(v["notes"]):
                    clean[key] = v["text"]
                    hits.append({"path": key, "phrase": "明確倉位", "replacement": "（需經 A4 裁決）"})

    clean["guard"] = {
        "passed": not hits and not missing,
        "blocked": hits,
        "broken_evidence": missing,
        "a4_gate": gate,
        "a4_connected": a4_connected,
        "advisory": gate == "advisory_only",
        "checked_phases": settings.banned_phrases,
        "note": (
            "A4 風控未接入：A5 只有建議權，輸出不構成下單指令。"
            if gate == "advisory_only" else
            "A4 已回報風控結果：本頁仍為研究輔助，最終執行需使用者確認。"
        ),
    }
    if hits:
        clean["guard"]["note"] += " 已攔截 %d 處不當表述。" % len(hits)
    if missing:
        clean["guard"]["note"] += " 有 %d 個證據 ID 無法追溯，相關結論請勿採用。" % len(missing)

    clean["disclaimer"] = (
        DISCLAIMER + " PrismQuant 不代客理財、不託管資金；"
        "所有方向標籤為概率判斷，可能出錯；政治人物表態的市場影響僅為概率標籤，非因果證明。"
    )
    return clean
