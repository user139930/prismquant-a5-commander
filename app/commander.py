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

# A1 / A2 未連線時的內建示例資料（獨立演示用，會明確標示為示例）
SAMPLE_NEWS = {
    "events": [
        {"title": "示例：美聯儲官員暗示若通脹持續回落將放緩加息節奏",
         "sentiment": "利多", "topics": ["利率"], "summary": "（示例新聞）"},
        {"title": "示例：芯片出口新規引發供應鏈擔憂",
         "sentiment": "利空", "topics": ["半導體", "供應鏈"], "summary": "（示例新聞）"},
        {"title": "示例：原油庫存意外下降帶動油價走高",
         "sentiment": "利多", "topics": ["能源"], "summary": "（示例新聞）"},
    ]
}

SAMPLE_POLITICAL = {
    "events": [
        {"title": "示例：央行官員談通脹與貨幣政策前景",
         "summary": "通脹壓力正在緩和，貨幣政策仍有進一步調整空間。",
         "direction": "中性", "markets": ["美股", "外匯"]},
        {"title": "示例：貿易代表談關鍵行業關稅措施",
         "summary": "將重新評估對關鍵行業的關稅措施，並與主要夥伴磋商。",
         "direction": "利空", "markets": ["美股", "港股"]},
        {"title": "示例：財政官員談金融科技監管",
         "summary": "計劃推出新一輪金融科技監管指引。",
         "direction": "中性", "markets": ["港股"]},
    ]
}


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
    if not a1_ok:
        a1 = SAMPLE_NEWS
    a2, a2_ok = _fetch_a2()
    if not a2_ok:
        a2 = SAMPLE_POLITICAL
    sample_mode = not (a1_ok and a2_ok)

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
        "sample_mode": sample_mode,
        "note": "部分上游（A1/A2）未連線，指揮官改用內建示例資料演示。" if sample_mode else "",
    }
