"""呼叫 A5（PrismQuant Commander）的合規檢查層。

用法：把這個檔案複製到 `winter_agents/` 目錄，然後在 `coordinator.py` 加兩行
（見同目錄的 PATCH.md）。

設計原則：任何失敗都回原文。這個模組是加分項，不該成為單點故障——
A5 沒開、逾時、回傳格式不對，Winter 都照常跑。
"""
from __future__ import annotations

import json
import logging
import os
from urllib.request import ProxyHandler, Request, build_opener

log = logging.getLogger(__name__)

#: A5 的位址。A5 換端口時改這個環境變數，不用動代碼。
A5_GUARD_URL = os.environ.get("A5_GUARD_URL", "http://127.0.0.1:8130/api/v1/guard")

#: 逾時秒數。合規檢查是同步阻塞的，寧可放棄也不要拖慢回話。
A5_GUARD_TIMEOUT = float(os.environ.get("A5_GUARD_TIMEOUT", "1.5"))

# 本機服務不能被 HTTP_PROXY 帶走（和專案裡其他對本機的呼叫同一個理由）
_opener = build_opener(ProxyHandler({}))


def guard_text(text: str, evidence_ids=None) -> str:
    """回傳清洗後的文字；任何異常都回原文。

    text：要檢查的正文（標題、段落、整份報告都可以）
    evidence_ids：你手上現有的證據編號清單；不傳就只做措辭改寫
    """
    if not isinstance(text, str) or not text.strip():
        return text

    try:
        body = json.dumps({
            "text": text,
            "evidence_ids": list(evidence_ids or []),
            "a4_connected": False,          # A4 接上後改 True，才會允許明確倉位
        }).encode("utf-8")
        req = Request(
            A5_GUARD_URL,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with _opener.open(req, timeout=A5_GUARD_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        # 連不上 / 逾時 / JSON 壞掉：一律放行原文
        log.debug("A5 合規層不可用（%s），本次使用原文", type(exc).__name__)
        return text

    cleaned = data.get("text")
    if not isinstance(cleaned, str) or not cleaned:
        return text                      # 回傳異常也放行原文

    blocked = data.get("blocked") or []
    broken = data.get("broken") or []
    if blocked or broken:
        log.info("A5 合規層：改寫 %d 處措辭、%d 個引用失效", len(blocked), len(broken))
    return cleaned
