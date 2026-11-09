# -*- coding: utf-8 -*-
"""A5 二次分析引擎（規則層）。

職責：把 A1 / A2 的原始卡片，變成一份「決策級」晨報。分三層：

    L0 正規化  把兩個 agent 不同 schema 的輸出攤平成同一種 atom，補上來源、時間、
               可追溯 ID。這一步不做判斷，只做對齊。
    L1 交叉驗證 同一主題的消息聚類，數「幾個獨立來源」講同一件事；A1 與 A2 方向
               相反時標成分歧，並寫明「什麼情況下這個分歧作廢」。
    L2 情境推演 由情緒分佈 + 交叉驗證強度推出基準／偏多／偏空三種路徑與觸發條件，
               再給出可解釋的置信度（每一分加減都列出來）。

為什麼要規則層而不是全部交給大模型：
1. 大模型超時或沒 key 時，系統必須降級而不是空白（proposal §3.1 明確要求）。
2. 置信度必須可重現、可被 examiner 追問；同一份輸入任何時候跑都要同一個分數。
3. 規則層算出「事實」，大模型只負責「表達」。模型不能自己發明事件。
"""
from __future__ import annotations

import datetime as dt
import re
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

HK_TZ = dt.timezone(dt.timedelta(hours=8))

# ---------------------------------------------------------------- 主題詞典

TOPICS: list[dict] = [
    {
        "key": "tariff", "label": "關稅與貿易",
        "kw": ["關稅", "貿易", "tariff", "出口限制", "出口管制", "制裁", "反傾銷", "貿易戰", "進口"],
        "channel": "成本與供應鏈",
        "assets": ["NDX", "SOX", "HSI", "USDCNH"],
        "trigger": "USTR 正式公告、豁免清單與生效日期",
        "invalidates": "若清單大幅豁免或延後生效，成本衝擊的假設不成立",
    },
    {
        "key": "rates", "label": "利率與貨幣政策",
        "kw": ["利率", "加息", "降息", "聯儲", "美聯儲", "fed", "fomc", "通脹", "cpi", "pce",
               "央行", "國債", "債息", "yield", "貨幣政策", "寬鬆", "緊縮"],
        "channel": "貼現率與估值",
        "assets": ["US10Y", "DXY", "NDX", "XAU"],
        "trigger": "下一次議息聲明、核心 CPI / PCE 讀數與點陣圖",
        "invalidates": "若通脹連續兩個月反彈，寬鬆路徑的定價要整個重估",
    },
    {
        "key": "tech", "label": "科技與半導體",
        "kw": ["芯片", "晶片", "半導體", "半导体", "ai", "人工智能", "nvidia", "台積電", "tsmc",
               "供應鏈", "服務器", "算力", "資本開支", "雲"],
        "channel": "資本開支與需求",
        "assets": ["SOX", "NVDA", "TSM", "NDX"],
        "trigger": "主要廠商的下季指引與設備交付數據",
        "invalidates": "若下游砍單或資本開支指引下修，需求邏輯反轉",
    },
    {
        "key": "energy", "label": "能源與商品",
        "kw": ["原油", "油價", "opec", "歐佩克", "天然氣", "黃金", "金價", "銅", "商品", "庫存", "增產"],
        "channel": "通脹與生產成本",
        "assets": ["CL", "XLE", "XAU", "US10Y"],
        "trigger": "OPEC+ 會議結果與每週原油庫存",
        "invalidates": "若庫存連續累積或增產落地，供給緊張的判斷失效",
    },
    {
        "key": "crypto", "label": "加密資產",
        "kw": ["比特幣", "btc", "以太", "eth", "crypto", "加密", "穩定幣", "區塊鏈", "coinbase"],
        "channel": "流動性與風險偏好",
        "assets": ["BTC", "ETH", "COIN"],
        "trigger": "儲備披露細則、ETF 資金流與交易所託管規則",
        "invalidates": "若監管細則轉鬆或資金流連續淨流入，風險折價收窄",
    },
    {
        "key": "geopolitics", "label": "地緣政治",
        "kw": ["地緣", "戰爭", "衝突", "選舉", "中東", "台海", "俄烏", "北約", "航運", "紅海", "封鎖"],
        "channel": "風險偏好與運費",
        "assets": ["VIX", "CL", "XAU", "BDI"],
        "trigger": "航運保險報價、運價指數與官方聲明",
        "invalidates": "若航線恢復正常且保費回落，風險溢價快速消退",
    },
    {
        "key": "regulation", "label": "監管與政策",
        "kw": ["監管", "反壟斷", "法規", "牌照", "證監", "sec", "合規", "指引", "諮詢", "罰款", "審查"],
        "channel": "風險溢價",
        "assets": ["HSI", "KWEB", "0700.HK", "9988.HK"],
        "trigger": "監管諮詢文件、生效日期與首批處罰案例",
        "invalidates": "若指引明確排除主流業務，最壞情境的折價可以收回",
    },
    {
        "key": "earnings", "label": "企業業績",
        "kw": ["財報", "業績", "盈利", "營收", "利潤", "guidance", "指引", "毛利率", "訂單"],
        "channel": "盈利預期",
        "assets": ["SPX", "NDX"],
        "trigger": "下一季財報與管理層指引",
        "invalidates": "若指引下修且訂單轉弱，盈利預期的上修不成立",
    },
    {
        "key": "hk", "label": "港股與中國",
        "kw": ["港股", "恒指", "恒生", "a股", "中國", "內地", "人民幣", "cnh", "南向", "政策"],
        "channel": "資金流與政策預期",
        "assets": ["HSI", "HSCEI", "USDCNH", "KWEB"],
        "trigger": "內地政策細則落地節奏與南向資金流向",
        "invalidates": "若細則力度低於預期或資金轉為淨流出，修復行情結束",
    },
    {
        "key": "fx", "label": "外匯",
        "kw": ["美元", "日圓", "日元", "歐元", "匯率", "dxy", "外匯", "日央行", "利差"],
        "channel": "相對價格與套息交易",
        "assets": ["DXY", "USDJPY", "EURUSD"],
        "trigger": "央行會議、利差變化與干預口風",
        "invalidates": "若利差重新擴大，套息交易的平倉壓力緩解",
    },
]

MACRO_TOPIC = {
    "key": "macro", "label": "宏觀與市場情緒",
    "channel": "風險偏好", "assets": ["SPX", "HSI", "XAU", "VIX"],
    "trigger": "下一個宏觀數據與成交量能",
    "invalidates": "若無新增催化，情緒面消息通常三個交易日內衰減",
}

TOPIC_BY_KEY = {t["key"]: t for t in TOPICS}
TOPIC_BY_KEY[MACRO_TOPIC["key"]] = MACRO_TOPIC

# 決定 primary topic 時的優先序（越前越優先）：越「事件性」越先
PRIORITY = ["tariff", "rates", "tech", "energy", "crypto", "geopolitics",
            "regulation", "earnings", "hk", "fx"]

PUBLISHERS = {
    "reuters.com": "Reuters", "bloomberg.com": "Bloomberg", "bbc.co.uk": "BBC",
    "bbc.com": "BBC", "scmp.com": "SCMP", "caixin.com": "財新", "caixinglobal.com": "財新",
    "xinhuanet.com": "新華社", "news.cn": "新華社", "ft.com": "FT", "wsj.com": "WSJ",
    "cnbc.com": "CNBC", "federalreserve.gov": "Federal Reserve", "ecb.europa.eu": "ECB",
    "t.me": "Telegram", "telegram.org": "Telegram", "cls.cn": "財聯社", "yicai.com": "第一財經",
}

GENERIC_SOURCES = {"", "rss", "示例", "sample", "unknown", "?"}

POSITIVE = {"利多", "利好", "bullish", "positive", "偏多"}
NEGATIVE = {"利空", "看空", "bearish", "negative", "偏空"}
NEUTRAL = {"中性", "neutral", "中立"}


# ---------------------------------------------------------------- 小工具

def _now() -> dt.datetime:
    return dt.datetime.now(HK_TZ)


def _as_text(*parts) -> str:
    return " ".join(str(p) for p in parts if p).lower()


def _matches(text: str, keywords: list[str]) -> int:
    return sum(1 for k in keywords if k.lower() in text)


def classify(*parts) -> tuple[str, list[str]]:
    """回傳 (primary_key, 全部命中的 key)。全部不命中就當宏觀。"""
    text = _as_text(*parts)
    hits = [t["key"] for t in TOPICS if _matches(text, t["kw"]) > 0]
    if not hits:
        return "macro", []
    hits.sort(key=lambda k: (-_matches(text, TOPIC_BY_KEY[k]["kw"]), PRIORITY.index(k) if k in PRIORITY else 99))
    return hits[0], hits


def publisher_of(source: str, url: str) -> str:
    """A1 現時只寫 source='RSS'，真正的媒體名要從 URL 反推——這是 A5 補的洞。"""
    src = (source or "").strip()
    if src.lower() not in GENERIC_SOURCES:
        return src
    host = (urlsplit(url).hostname or "").lower()
    for domain, name in PUBLISHERS.items():
        if host == domain or host.endswith("." + domain):
            return name
    if host:
        return host[4:] if host.startswith("www.") else host
    return "未標明來源"


def parse_time(value) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=HK_TZ)
    s = str(value).strip()
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=HK_TZ)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(s)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=HK_TZ)
    except Exception:
        return None


def age_hours(value) -> float | None:
    t = parse_time(value)
    if t is None:
        return None
    return round((_now() - t.astimezone(HK_TZ)).total_seconds() / 3600.0, 1)


def _sentiment_score(label: str) -> float:
    s = (label or "").strip()
    if s in POSITIVE:
        return 1.0
    if s in NEGATIVE:
        return -1.0
    return 0.0


def _dir_word(score: float) -> str:
    if score >= 0.18:
        return "偏多"
    if score <= -0.18:
        return "偏空"
    return "中性"


def _strength_word(value: float) -> str:
    if value >= 0.66:
        return "強"
    if value >= 0.4:
        return "中"
    return "弱"


def _pick(items: list[dict], n: int) -> list[dict]:
    return sorted(items, key=lambda x: (x.get("confidence") or 0, -(x.get("age_hours") or 0)), reverse=True)[:n]


def _join_titles(items: list[dict], n: int = 2) -> str:
    titles = [i["title"] for i in _pick(items, n)]
    return "、".join("「%s」" % t for t in titles)


# ---------------------------------------------------------------- L0 正規化

def normalize_a1(events: list[dict], agent: str = "A1") -> list[dict]:
    atoms = []
    for e in events or []:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        summary = (e.get("summary") or "").strip()
        topics = e.get("topics") or []
        if isinstance(topics, str):
            topics = [topics]
        key, all_keys = classify(title, summary, " ".join(map(str, topics)))
        insts = e.get("instruments") or []
        if isinstance(insts, str):
            insts = [insts]
        atoms.append({
            "id": None,
            "agent": agent,
            "title": title,
            "summary": summary,
            "topics": [str(t) for t in topics][:6],
            "topic_key": key,
            "topic_keys": all_keys or [key],
            "instruments": [str(i) for i in insts][:6],
            "sentiment": (e.get("sentiment") or "中性").strip(),
            "direction": _sentiment_score(e.get("sentiment")),
            "confidence": float(e["confidence"]) if e.get("confidence") is not None else 0.5,
            "publisher": publisher_of(e.get("source", ""), e.get("url", "")),
            "url": e.get("url") or "",
            "published": e.get("published") or "",
            "age_hours": age_hours(e.get("published")),
            "sampled": bool(e.get("sampled")),
        })
    return atoms


def normalize_a2(events: list[dict], agent: str = "A2") -> list[dict]:
    atoms = []
    for e in events or []:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        summary = (e.get("summary") or "").strip()
        entities = e.get("entities") or []
        if isinstance(entities, str):
            entities = [entities]
        markets = e.get("markets") or []
        if isinstance(markets, str):
            markets = [markets]
        key, all_keys = classify(title, summary, " ".join(map(str, markets)))
        direction = (e.get("direction") or "中性").strip()
        atoms.append({
            "id": None,
            "agent": agent,
            "title": title,
            "summary": summary,
            "topics": [str(m) for m in markets][:6],
            "topic_key": key,
            "topic_keys": all_keys or [key],
            "instruments": [str(m) for m in markets][:6],
            "sentiment": direction,
            "direction": _sentiment_score(direction),
            "confidence": float(e["confidence"]) if e.get("confidence") is not None else 0.5,
            "publisher": "A2 公開發言",
            "url": e.get("original_url") or "",
            "published": e.get("published_at") or "",
            "age_hours": age_hours(e.get("published_at")),
            "entities": [str(x) for x in entities][:6],
            "sampled": False,
        })
    return atoms


def assign_ids(atoms: list[dict]) -> None:
    counters = {"A1": 0, "A2": 0}
    for a in atoms:
        counters[a["agent"]] = counters.get(a["agent"], 0) + 1
        prefix = "N" if a["agent"] == "A1" else "P"
        a["id"] = "%s%d" % (prefix, counters[a["agent"]])


# ---------------------------------------------------------------- 資料充分性

def data_quality(atoms: list[dict], sample_mode: bool) -> dict:
    pub_count: dict[str, int] = {}
    for a in atoms:
        if a["agent"] != "A1":
            continue
        pub_count[a["publisher"]] = pub_count.get(a["publisher"], 0) + 1
    publishers = [{"name": k, "count": v} for k, v in sorted(pub_count.items(), key=lambda kv: -kv[1])]

    # 每個主題有幾個獨立媒體在講（只有 A1 有媒體維度）
    cover: dict[str, set[str]] = {}
    for a in atoms:
        if a["agent"] != "A1":
            continue
        cover.setdefault(a["topic_key"], set()).add(a["publisher"])
    single_source = sorted([TOPIC_BY_KEY[k]["label"] for k, v in cover.items() if len(v) == 1])

    ages = [a["age_hours"] for a in atoms if a["age_hours"] is not None]
    newest = min(ages) if ages else None
    oldest = max(ages) if ages else None

    notes = []
    if len(publishers) <= 1:
        notes.append("新聞面只有 %d 個獨立媒體，交叉驗證不成立；A1 需要補 Reuters / Bloomberg / SCMP / 財新 與 Telegram 頻道。" % len(publishers))
    if single_source:
        notes.append("以下主題只有單一來源，已自動降級：%s。" % "、".join(single_source))
    if newest is not None and newest > 6:
        notes.append("最新一條消息已是 %.1f 小時前，盤前資訊可能不完整。" % newest)
    if sample_mode:
        notes.append("A1 / A2 未連線，本頁使用內建示例資料（已標示），不可用作真實決策。")

    return {
        "publishers": publishers,
        "distinct_publishers": len(publishers),
        "single_source_topics": single_source,
        "newest_age_hours": newest,
        "oldest_age_hours": oldest,
        "notes": notes,
        "sample_mode": sample_mode,
        "a1_count": sum(1 for a in atoms if a["agent"] == "A1"),
        "a2_count": sum(1 for a in atoms if a["agent"] == "A2"),
    }


# ---------------------------------------------------------------- L1 聚類 + 交叉驗證

def cluster(atoms: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for a in atoms:
        groups.setdefault(a["topic_key"], []).append(a)

    clusters = []
    for key, items in groups.items():
        meta = TOPIC_BY_KEY.get(key, MACRO_TOPIC)
        a1 = [i for i in items if i["agent"] == "A1"]
        a2 = [i for i in items if i["agent"] == "A2"]
        pubs = {i["publisher"] for i in a1}
        weights = [abs(i["direction"]) * (i["confidence"] or 0.5) for i in items]
        total_w = sum(weights) or 1.0
        score = sum(i["direction"] * (i["confidence"] or 0.5) for i in items) / total_w
        agreement = sum(1 for i in items if (i["direction"] > 0) == (score > 0)) / len(items) if items else 0
        strength = 0.0
        if a1:
            strength += min(len(pubs), 3) / 3 * 0.55
        if a2:
            strength += 0.2
        strength += agreement * 0.25
        if len(pubs) <= 1 and a1:
            strength *= 0.75

        clusters.append({
            "topic_key": key,
            "label": meta["label"],
            "channel": meta["channel"],
            "atoms": items,
            "a1": a1,
            "a2": a2,
            "publishers": sorted(pubs),
            "score": round(score, 3),
            "direction": _dir_word(score),
            "strength": round(min(strength, 1.0), 3),
            "agreement": round(agreement, 2),
        })
    clusters.sort(key=lambda c: (c["strength"], len(c["atoms"])), reverse=True)
    return clusters


def detect_conflicts(clusters: list[dict]) -> list[dict]:
    out = []
    for c in clusters:
        a1_score = _mean([a["direction"] * (a["confidence"] or 0.5) for a in c["a1"]]) if c["a1"] else 0.0
        a2_score = _mean([a["direction"] * (a["confidence"] or 0.5) for a in c["a2"]]) if c["a2"] else 0.0
        if not c["a1"] or not c["a2"]:
            continue
        if abs(a1_score) < 0.15 or abs(a2_score) < 0.15:
            continue
        if a1_score * a2_score >= 0:
            continue
        out.append({
            "topic": c["label"],
            "a1": "新聞面%s（%s）" % (_dir_word(a1_score), _join_titles(c["a1"], 2)),
            "a2": "政策面%s（%s）" % (_dir_word(a2_score), _join_titles(c["a2"], 2)),
            "a1_evidence": [a["id"] for a in _pick(c["a1"], 2)],
            "a2_evidence": [a["id"] for a in _pick(c["a2"], 2)],
            "resolution": (
                "按 PrismQuant 衝突規則，事件驅動的政策面（A2）優先於新聞面（A1）："
                "該組只下調風險預算、不反向開倉，等 A2 的正式文件落地再看。"
            ),
            "breaks_if": (
                "若 48 小時內 A2 沒有新增表態，且 A1 出現兩個以上獨立媒體同向報導，"
                "此分歧降級為噪音，回到新聞面主導。"
            ),
            "severity": round(min(abs(a1_score), abs(a2_score)), 2),
        })
    out.sort(key=lambda x: -x["severity"])
    return out


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ---------------------------------------------------------------- L2 傳導 + 情境

def transmission(clusters: list[dict]) -> list[dict]:
    rows = []
    for c in clusters:
        if c["strength"] < 0.2:
            continue
        instruments: list[str] = []
        for a in c["atoms"]:
            for i in a["instruments"]:
                if i not in instruments:
                    instruments.append(i)
        assets = instruments[:4] or TOPIC_BY_KEY.get(c["topic_key"], MACRO_TOPIC)["assets"][:4]
        rows.append({
            "from": c["label"],
            "channel": c["channel"],
            "to": assets,
            "direction": c["direction"],
            "strength": _strength_word(c["strength"]),
            "strength_value": c["strength"],
            "horizon": "3–10 個交易日" if c["strength"] >= 0.6 else "1–4 週",
            "evidence": [a["id"] for a in _pick(c["atoms"], 3)],
            "note": "已交叉驗證 %d 個媒體" % len(c["publishers"]) if len(c["publishers"]) > 1
                    else "單一來源，方向僅供參考",
        })
    return rows[:6]


def scenarios(clusters: list[dict], conflicts: list[dict]) -> list[dict]:
    bull = sum(1 for c in clusters for a in c["atoms"] if a["direction"] > 0)
    bear = sum(1 for c in clusters for a in c["atoms"] if a["direction"] < 0)
    total = max(bull + bear, 1)
    net = (bull - bear) / total

    p_bull = round(min(0.45, max(0.10, 0.28 + 0.16 * net)), 2)
    p_bear = round(min(0.45, max(0.10, 0.28 - 0.16 * net)), 2)
    p_base = round(1 - p_bull - p_bear, 2)

    top = clusters[0] if clusters else None
    top_label = top["label"] if top else "宏觀情緒"

    return [
        {
            "name": "基準情境",
            "prob": p_base,
            "path": "消息面維持現有方向但沒有新增催化，%s 的定價逐步消化，指數區間震盪。" % top_label,
            "triggers": ["沒有新的官方文件或數據意外", "成交量能維持在近 20 日均值附近"],
        },
        {
            "name": "偏多情境",
            "prob": p_bull,
            "path": "利多消息獲得第二個獨立來源確認，風險偏好回升，%s 相關標的先於大盤走強。" % top_label,
            "triggers": ["同主題出現兩個以上獨立媒體同向報導", "波動率回落、資金回流風險資產"],
        },
        {
            "name": "偏空情境",
            "prob": p_bear,
            "path": "政策面轉鷹或事件升級，%s 的風險溢價擴大，高估值板塊先被殺估值。" % top_label,
            "triggers": ["A2 出現升級表態或正式限制文件", "油價／波動率同步上行"],
        },
    ]


def confidence_score(quality: dict, conflicts: list[dict], clusters: list[dict], sample_mode: bool) -> dict:
    score = 0.45
    reasons: list[str] = ["基礎分 0.45（中等偏低：只覆蓋新聞與政策兩條線，沒有行情與倉位數據）"]

    pub = quality["distinct_publishers"]
    if pub >= 3:
        score += 0.12
        reasons.append("+0.12 新聞面有 %d 個獨立媒體，交叉驗證成立" % pub)
    elif pub == 2:
        score += 0.05
        reasons.append("+0.05 只有 2 個獨立媒體，交叉驗證勉強成立")
    else:
        score -= 0.08
        reasons.append("-0.08 新聞面只有 %d 個獨立媒體，無法交叉驗證" % pub)

    if quality["a2_count"] > 0:
        score += 0.08
        reasons.append("+0.08 A2 政策面有 %d 條可引用表態" % quality["a2_count"])
    else:
        score -= 0.12
        reasons.append("-0.12 A2 未連線，政策風險維度缺失")

    if conflicts:
        penalty = min(0.12, 0.05 * len(conflicts))
        score -= penalty
        reasons.append("-%.2f %d 組 A1／A2 方向衝突，結論需要保留兩面" % (penalty, len(conflicts)))

    if quality["single_source_topics"]:
        score -= 0.06
        reasons.append("-0.06 存在單一來源主題：%s" % "、".join(quality["single_source_topics"]))

    newest = quality["newest_age_hours"]
    if newest is not None and newest > 24:
        score -= 0.10
        reasons.append("-0.10 最新消息已 %.1f 小時前，時效性不足" % newest)
    elif newest is not None:
        reasons.append("+0.00 最新消息 %.1f 小時內，時效正常" % newest)

    if sample_mode:
        score = min(score, 0.55)
        reasons.append("上限 0.55：示例資料，不能當真實結論")

    score = round(max(0.15, min(0.85, score)), 2)
    level = "高" if score >= 0.7 else ("中" if score >= 0.5 else "低")
    return {"score": score, "level": level, "reasons": reasons}


# ---------------------------------------------------------------- 文字組裝

def compose_sections(clusters, conflicts, quality, watch_terms) -> dict:
    happened: list[str] = []
    for c in clusters[:4]:
        parts = []
        if c["a1"]:
            parts.append("%d 條新聞（%s）" % (len(c["a1"]), "、".join(c["publishers"][:3]) or "未標明來源"))
        if c["a2"]:
            parts.append("%d 條政策表態（A2）" % len(c["a2"]))
        happened.append(
            "%s：%s，核心是%s，整體%s（證據 %s）。" % (
                c["label"], " ＋ ".join(parts) or "無來源",
                _join_titles(c["atoms"], 2), c["direction"],
                "、".join(a["id"] for a in _pick(c["atoms"], 3)))
        )
    if not happened:
        happened.append("上游沒有可用的新消息，本時段沒有需要處理的變化。")

    so_what: list[str] = []
    for row in transmission(clusters)[:3]:
        so_what.append(
            "「%s」若延續，傳導通道是%s，首當其衝的是 %s，持有期看 %s，強度%s（%s；證據 %s）。" % (
                row["from"], row["channel"], "／".join(row["to"]), row["horizon"],
                row["strength"], row["note"], "、".join(row["evidence"]))
        )
    for cf in conflicts[:2]:
        so_what.append(
            "「%s」上兩個 agent 方向相反：%s，%s。%s（證據 %s）" % (
                cf["topic"], cf["a1"], cf["a2"], cf["resolution"],
                "、".join(cf["a1_evidence"] + cf["a2_evidence"]))
        )
    if quality["single_source_topics"]:
        so_what.append(
            "單一來源的主題（%s）已壓低權重：同一件事只有一家在講，通常是標題效應而不是趨勢。"
            % "、".join(quality["single_source_topics"])
        )
    if not so_what:
        so_what.append("沒有足夠的交叉驗證，暫不下判斷；等第二個來源出現再談方向。")

    watch: list[dict] = []
    for c in clusters[:3]:
        meta = TOPIC_BY_KEY.get(c["topic_key"], MACRO_TOPIC)
        watch.append({
            "item": meta["label"],
            "why": "目前%s、強度%s，是本期最需要盯的一條線" % (c["direction"], _strength_word(c["strength"])),
            "trigger": meta["trigger"],
            "invalidates": meta["invalidates"],
        })
    for term in watch_terms[:2]:
        if all(term not in w["item"] for w in watch):
            meta = topic_for_term(term)
            watch.append({
                "item": term,
                "why": "在關注詞列表內，命中「%s」這條線" % (meta["label"] if meta else "宏觀"),
                "trigger": meta["trigger"] if meta else "下一個相關官方表態或數據",
                "invalidates": meta["invalidates"] if meta else "若連續三個交易日無新增消息，移出關注",
            })
    if not watch:
        watch.append({
            "item": "上游採集",
            "why": "本時段 A1／A2 都沒有可用消息，這是唯一需要盯的事",
            "trigger": "下一次採集是否恢復（A1 /health、A2 /health）",
            "invalidates": "一旦任一邊恢復並帶回新消息，立即重算本頁",
        })
    return {"what_happened": happened, "so_what": so_what, "watch": watch[:5]}


def topic_for_term(term: str) -> dict | None:
    t = (term or "").lower()
    if not t:
        return None
    for topic in TOPICS:
        if t in topic["label"].lower() or any(t in k.lower() or k.lower() in t for k in topic["kw"]):
            return topic
    return None


def reading_minutes(sections: dict, brief: str = "") -> float:
    text = brief + " ".join(sections.get("what_happened", [])) + " ".join(sections.get("so_what", []))
    text += " ".join(str(w.get("why", "")) + str(w.get("trigger", "")) for w in sections.get("watch", []))
    chars = len(re.sub(r"\s", "", text))
    return round(max(0.5, chars / 400.0), 1)


# ---------------------------------------------------------------- 主入口：晨報

def build_brief(question: str, a1_events, a2_events, sample_mode: bool,
                watchlist: list[str], upstream_meta: dict) -> dict:
    a1 = normalize_a1(a1_events)
    a2 = normalize_a2(a2_events)
    atoms = a1 + a2
    assign_ids(atoms)

    quality = data_quality(atoms, sample_mode)
    clusters = cluster(atoms)
    conflicts = detect_conflicts(clusters)
    watch_terms = [t for t in (watchlist or []) if any(t.lower() in _as_text(a["title"], a["summary"]) for a in atoms)]

    sections = compose_sections(clusters, conflicts, quality, watch_terms)
    conf = confidence_score(quality, conflicts, clusters, sample_mode)

    if conf["level"] == "高":
        headline = "%s主導，方向可跟但不宜追價" % (clusters[0]["label"] if clusters else "宏觀情緒")
    elif conf["level"] == "中":
        if conflicts:
            headline = "%s是主線，但有 %d 組 A1／A2 分歧，先降風險" % (
                clusters[0]["label"] if clusters else "宏觀情緒", len(conflicts))
        elif quality["distinct_publishers"] >= 3:
            headline = "%s是主線，%d 個獨立媒體交叉驗證，可跟蹤" % (
                clusters[0]["label"] if clusters else "宏觀情緒", quality["distinct_publishers"])
        else:
            headline = "%s是主線，但只有 %d 個獨立來源，先觀察再行動" % (
                clusters[0]["label"] if clusters else "宏觀情緒", quality["distinct_publishers"])
    else:
        headline = "資訊不足：%s，本期只做風險提示" % (
            clusters[0]["label"] if clusters else "沒有可用消息")

    evidence = []
    for a in sorted(atoms, key=lambda x: (x["agent"] != "A1", -(x["confidence"] or 0))):
        evidence.append({
            "id": a["id"], "agent": a["agent"], "title": a["title"],
            "summary": (a["summary"] or "")[:160],
            "publisher": a["publisher"], "url": a["url"],
            "sentiment": a["sentiment"], "confidence": a["confidence"],
            "age_hours": a["age_hours"], "published": a["published"],
            "topic": TOPIC_BY_KEY.get(a["topic_key"], MACRO_TOPIC)["label"],
            "instruments": a["instruments"],
        })

    return {
        "kind": "brief",
        "question": question,
        "headline": headline,
        "sections": sections,
        "transmission": transmission(clusters),
        "conflicts": conflicts,
        "scenarios": scenarios(clusters, conflicts),
        "evidence": evidence,
        "confidence": conf,
        "data_quality": quality,
        "watch_terms": watch_terms,
        "reading_minutes": reading_minutes(sections),
        "upstream": upstream_meta,
        "clusters": [{"label": c["label"], "direction": c["direction"], "strength": c["strength"],
                      "count": len(c["atoms"]), "publishers": c["publishers"]} for c in clusters],
    }
