# -*- coding: utf-8 -*-
"""A5 指揮官：呼叫 A1 + A2，匯總、調解分歧，產出晨報與證據鏈。"""
import httpx

from app.config import settings
from app.llm import chat_json

SYSTEM = (
    "你是 PrismQuant 的指揮官（Commander）。你只匯總 A1 新聞與 A2 政要監控的結果，"
    "產出給訂閱者 10 分鐘讀得完的晨報。要求："
    "1) 直接、不誇大，證據不足就明說；"
    "2) 每條結論都要能追溯到是哪個 agent 給的（證據鏈）；"
    "3) A1 與 A2 意見衝突時，兩邊都要如實列出，不偏袒；"
    "4) 不給「一定會漲」這種保證。"
    "注意：A3 執行與 A4 風控目前尚未接入，風控部分以一句話聲明即可。"
)


def _fetch_a1(limit: int = 6):
    try:
        r = httpx.get(settings.a1_api_url + "/api/v1/news/events", params={"limit": limit}, timeout=12)
        r.raise_for_status()
        return r.json(), True
    except Exception:
        return None, False


def _fetch_a2(limit: int = 10):
    try:
        r = httpx.get(settings.a2_api_url + "/api/v1/analyses", params={"limit": limit}, timeout=12)
        r.raise_for_status()
        return r.json(), True
    except Exception:
        return None, False


def _fmt_a1(a1: dict) -> str:
    rows = []
    for e in (a1 or {}).get("events", []):
        rows.append("- [A1] %s（%s）主題：%s" % (
            e.get("title", ""), e.get("sentiment", ""), "/".join(e.get("topics", []))))
    return "\n".join(rows) or "（無）"


def _fmt_a2(a2: dict) -> str:
    rows = []
    for e in (a2 or {}).get("events", []):
        rows.append("- [A2] %s：%s → %s，影響：%s" % (
            e.get("title", ""), e.get("summary", ""), e.get("direction", ""),
            "/".join(e.get("markets", []))))
    return "\n".join(rows) or "（無）"


def run(question: str = ""):
    a1, a1_ok = _fetch_a1()
    a2, a2_ok = _fetch_a2()

    q = question.strip() or "生成今天的晨報"
    prompt = (
        "用戶的問題：%s\n\n"
        "以下是 A1 新聞情報與 A2 政要監控的原始結論：\n"
        "=== A1 新聞 ===\n%s\n\n=== A2 政要監控 ===\n%s\n\n"
        "請輸出一個 JSON，格式：\n"
        '{"headline":"一句話結論",'
        '"brief":"晨報正文，2-3 段中文，講清楚：市場在發生什麼、有什麼事件風險、建議留意什麼",'
        '"evidence":[{"agent":"A1 新聞情報","point":"..."},{"agent":"A2 政要監控","point":"..."}],'
        '"disagreements":[{"topic":"...","a1":"...","a2":"..."}],'
        '"risk_note":"一句風險聲明（例如：A4 風控未接入，以上僅供研究參考）"}'
        % (q, _fmt_a1(a1), _fmt_a2(a2))
    )
    brief = chat_json(SYSTEM, prompt)
    if not isinstance(brief, dict):
        brief = {}

    return {
        "agent": "A5 指揮官",
        "question": q,
        "headline": brief.get("headline", ""),
        "brief": brief.get("brief", ""),
        "evidence": brief.get("evidence", []),
        "disagreements": brief.get("disagreements", []),
        "risk_note": brief.get("risk_note", ""),
        "upstream_ok": {"a1": a1_ok, "a2": a2_ok},
    }
