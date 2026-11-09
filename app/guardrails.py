# -*- coding: utf-8 -*-
"""合規護欄：A5 對外說話前的最後一道閘。

三件事：
1. 禁語掃描——「必漲」「穩賺」「內幕」這類表述直接刪句，不讓它出現在任何對外文字裡。
2. 證據可追溯——傳導鏈引用的每個 ID 必須真的存在於證據清單，否則標為 broken。
3. 執行邊界——A4 未接入時，A5 只有建議權，輸出必須帶 advisory_only 標記。
"""
from __future__ import annotations

import copy
import re

from app.config import settings

PLACEHOLDER = "［已按合規規則刪除不當表述］"


def scan(text: str) -> list[str]:
    return [p for p in settings.banned_phrases if p and p.lower() in (text or "").lower()]


def _clean(text: str, hits: list[dict], path: str) -> str:
    found = scan(text)
    if not found:
        return text
    for phrase in found:
        hits.append({"path": path, "phrase": phrase})
    # 按句刪除，保留句號，避免整段消失
    sentences = re.split(r"(?<=[。！？!?\n])", text)
    kept = []
    for s in sentences:
        kept.append(PLACEHOLDER if scan(s) else s)
    return "".join(kept)


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
    gate = "risk_checked" if (a4_connected and settings.a4_gate == "risk_checked") else "advisory_only"

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
        "PrismQuant 為研究與決策輔助工具，不代客理財、不託管資金、不構成投資建議。"
        "所有方向標籤為概率判斷，可能出錯；政治人物表態的市場影響僅為概率標籤，非因果證明。"
    )
    return clean
