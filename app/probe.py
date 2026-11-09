# -*- coding: utf-8 -*-
"""單篇新聞探針：貼一段新聞進來，直接出「二次分析」結果。

隊友的要求是「餵一篇新聞，show output 睇下」。所以這裡不是摘要器，而是回答四個問題：

    1. 這條消息屬於哪條線（主題），方向是甚麼？
    2. 它怎麼傳導到具體標的（通道、方向、時間尺度、強度）？
    3. 上游 A1 / A2 是不是已經講過同一件事（重複 or 新增）？
    4. 值不值得升級去 A3 觀察，以及「甚麼情況下這個判斷作廢」？

規則層先做，因為探針必須能在沒 key、沒網絡的情況下示範。
"""
from __future__ import annotations

import re

from app.analyze import (
    MACRO_TOPIC,
    TOPIC_BY_KEY,
    _as_text,
    _dir_word,
    _strength_word,
    classify,
    publisher_of,
)

# 方向詞典：同一組詞在不同主題下方向可以相反，所以要分主題
BULL_WORDS = ["上調", "上修", "超預期", "回升", "增長", "創新高", "利多", "利好", "放寬", "降息",
              "寬鬆", "補貼", "刺激", "復甦", "突破", "訂單增加", "資本開支上調"]
BEAR_WORDS = ["下調", "下修", "低於預期", "下滑", "衰退", "利空", "收緊", "加息", "限制", "制裁",
              "加徵", "罰款", "裁員", "砍單", "虧損", "違約"]

TOPIC_OVERRIDE = {
    "energy": {
        "bull": ["減產", "推遲增產", "庫存下降", "供應中斷", "封鎖", "停產"],
        "bear": ["增產", "庫存增加", "釋放儲備", "需求疲弱", "復產"],
    },
    "tariff": {
        "bull": ["豁免", "取消關稅", "下調關稅", "達成協議", "休戰"],
        "bear": ["提高關稅", "加徵", "出口限制", "制裁", "清單", "反傾銷"],
    },
    "rates": {
        "bull": ["降息", "放緩加息", "寬鬆", "暫停加息"],
        "bear": ["加息", "鷹", "收緊", "縮表", "通脹反彈"],
    },
    "crypto": {
        "bull": ["批准", "資金流入", "採用", "儲備披露"],
        "bear": ["禁止", "起訴", "罰款", "下架", "擠兌"],
    },
}

STOPWORDS = {"the", "and", "for", "with", "that", "this", "will", "from", "have", "has", "are", "was"}


def _tokens(text: str) -> set[str]:
    text = (text or "").lower()
    latin = re.findall(r"[a-z]{3,}", text)
    cjk = re.findall(r"[\u4e00-\u9fff]{2}", text)
    return {t for t in latin + cjk if t not in STOPWORDS}


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _direction(text: str, topic: str) -> tuple[float, str]:
    t = (text or "").lower()
    bull = sum(1 for w in BULL_WORDS if w in t)
    bear = sum(1 for w in BEAR_WORDS if w in t)
    override = TOPIC_OVERRIDE.get(topic, {})
    bull += sum(1 for w in override.get("bull", []) if w in t) * 2
    bear += sum(1 for w in override.get("bear", []) if w in t) * 2
    score = (bull - bear) / float(max(bull + bear, 1))
    return round(score, 2), _dir_word(score)


def analyze(title: str, text: str, source: str = "", url: str = "",
            existing: list[dict] | None = None, watchlist: list[str] | None = None) -> dict:
    body = (text or "").strip()
    headline = (title or "").strip() or (body[:40] + "…" if body else "（未命名消息）")
    topic, extra = classify(headline, body)
    meta = TOPIC_BY_KEY.get(topic, MACRO_TOPIC)

    score, direction = _direction(headline + " " + body, topic)
    pub = publisher_of(source, url)

    # 與上游既有事件比對：同一件事是否已經被 A1 / A2 講過
    matches = []
    for atom in existing or []:
        sim = _similarity(headline + " " + body[:200], atom.get("title", "") + " " + atom.get("summary", ""))
        if atom.get("topic_key") == topic:
            sim += 0.15
        if sim >= 0.3:
            matches.append({
                "id": atom.get("id"), "agent": atom.get("agent"), "title": atom.get("title"),
                "publisher": atom.get("publisher"), "similarity": round(min(sim, 0.99), 2),
            })
    matches.sort(key=lambda m: -m["similarity"])

    # 同主題但不是同一件事：也要講清楚，否則使用者會以為上游完全沒提過
    matched_ids = {m["id"] for m in matches}
    same_topic = [{
        "id": a.get("id"), "agent": a.get("agent"), "title": a.get("title"),
        "publisher": a.get("publisher"),
    } for a in (existing or [])
        if a.get("topic_key") == topic and a.get("id") not in matched_ids][:3]

    publishers = {pub}
    for m in matches:
        if m.get("publisher"):
            publishers.add(m["publisher"])

    strength = 0.25
    if len(publishers) >= 2:
        strength += 0.25
    if len(publishers) >= 3:
        strength += 0.15
    if abs(score) >= 0.5:
        strength += 0.15
    if matches:
        strength += 0.1
    strength = round(min(strength, 1.0), 2)

    if score > 0.15:
        chain_dir = "偏多"
    elif score < -0.15:
        chain_dir = "偏空"
    else:
        chain_dir = "中性"

    assets = (meta.get("assets") or [])[:4]
    chain = [
        {"step": "事件", "detail": headline, "evidence": "input"},
        {"step": "通道", "detail": "%s：%s" % (meta["channel"], "直接影響成本／估值／風險偏好" if chain_dir != "中性" else "方向未明，先記錄"),
         "evidence": "rule"},
        {"step": "標的", "detail": "、".join(assets), "evidence": "taxonomy"},
        {"step": "觀察", "detail": meta["trigger"], "evidence": "rule"},
    ]

    if strength >= 0.6 and chain_dir != "中性":
        verdict = "值得升級：交給 A3 觀察，等第二個來源確認"
        verdict_level = "escalate"
    elif matches and (matches[0]["similarity"] or 0) >= 0.5:
        verdict = "已有覆蓋：上游講過同一件事，不重複處理"
        verdict_level = "duplicate"
    elif chain_dir == "中性":
        verdict = "僅記錄：方向不明，先放入觀察池"
        verdict_level = "log"
    else:
        verdict = "僅記錄：只有 %s 一個來源，等第二個媒體確認再升級" % pub
        verdict_level = "log"

    conf_score = 0.3 + (0.2 if len(publishers) >= 2 else 0) + (0.15 if abs(score) >= 0.5 else 0)
    conf_score += 0.1 if matches else 0
    if len(publishers) == 1:
        conf_score -= 0.05
    conf_score = round(max(0.15, min(0.8, conf_score)), 2)

    hits = [w for w in (watchlist or []) if w.lower() in _as_text(headline, body)]

    return {
        "kind": "probe",
        "input": {"title": headline, "source": pub, "url": url, "chars": len(body),
                  "preview": body[:400]},
        "topic": meta["label"],
        "topic_keys": extra or [topic],
        "direction": direction,
        "direction_score": score,
        "strength": strength,
        "strength_word": _strength_word(strength),
        "verdict": verdict,
        "verdict_level": verdict_level,
        "chain": chain,
        "assets": [{"symbol": a, "direction": chain_dir, "horizon": "1–4 週" if strength < 0.6 else "3–10 個交易日"}
                   for a in assets],
        "corroboration": {
            "publishers": sorted(publishers),
            "matched": matches[:4],
            "same_topic": same_topic,
            "is_new": not matches,
            "note": (
                "上游已有 %d 條同事件報導，可視為交叉驗證" % len(matches) if matches else
                "同主題上游有 %d 條，但講的不是同一件事：屬同線新增，仍需第二個來源" % len(same_topic)
                if same_topic else
                "上游完全沒有同主題報導，屬全新線索，建議人工覆核一次"
            ),
        },
        "watch_hits": hits,
        "confidence": {
            "score": conf_score,
            "level": "高" if conf_score >= 0.7 else ("中" if conf_score >= 0.5 else "低"),
            "reasons": [
                "基礎 0.30（單條輸入，沒有持倉與行情維度）",
                "+0.20 有第二個來源" if len(publishers) >= 2 else "-0.05 只有 %s 一個來源" % pub,
                "+0.15 方向詞明確" if abs(score) >= 0.5 else "+0.00 方向詞不明確",
                "+0.10 與上游同主題報導吻合" if matches else "+0.00 上游未見同主題",
            ],
        },
        "what_would_change": [
            "出現第二個獨立媒體報導同一件事（方向會被確認或修正）",
            "官方文件落地：%s" % meta["trigger"],
            "反轉條件：%s" % meta["invalidates"],
        ],
        "caveats": [
            "規則版探針只做關鍵詞與主題映射，不判斷語氣與上下文；接上大模型後會補這一層。",
            "方向是概率標籤，不是價格預測，也不構成下單建議。",
        ],
    }
