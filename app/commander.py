# -*- coding: utf-8 -*-
"""A5 指揮官：唯一面向使用者的入口。

一次 run 的順序（也是報告裡可以畫的那張流程圖）：

    1. 取上游      A1 新聞 + A2 政要監控；任何一邊失敗 → 整份改用示例資料（不真假混用）
    2. 規則層      analyze.build_brief()：L0 正規化、L1 交叉驗證、L2 情境推演
    3. 大腦層      按 PQ_BRAIN_TOPOLOGY 決定要不要叫大模型；只讓它寫「表達」，不准改事實
    4. 護欄        禁語掃描、證據可追溯、A4 未接入 → advisory_only
    5. 留檔        Run Ledger 存 payload + 輸入快照，支援 replay 與漂移比對

任何一步失敗都不會讓整份報告空白：規則層永遠有話可說，這是 proposal §3.1 的降級要求。
"""
from __future__ import annotations

import time

from app import analyze, brains, connectors, guardrails, ledger, store, probe as probe_mod
from app.config import settings


# ---------------------------------------------------------------- 文字組裝

def compose_text(payload: dict) -> str:
    s = payload.get("sections") or {}
    lines = ["【主線】%s" % payload.get("headline", "")]
    lines.append("")
    lines.append("【發生了什麼】")
    for i, item in enumerate(s.get("what_happened", []), 1):
        lines.append("%d. %s" % (i, item))
    lines.append("")
    lines.append("【這意味著什麼】")
    for i, item in enumerate(s.get("so_what", []), 1):
        lines.append("%d. %s" % (i, item))
    lines.append("")
    lines.append("【要盯什麼】")
    for w in s.get("watch", []):
        lines.append("· %s：%s；觸發 %s；作廢條件 %s" % (
            w.get("item", ""), w.get("why", ""), w.get("trigger", ""), w.get("invalidates", "")))
    if payload.get("conflicts"):
        lines.append("")
        lines.append("【分歧】")
        for c in payload["conflicts"]:
            lines.append("· %s｜A1：%s｜A2：%s" % (c["topic"], c["a1"], c["a2"]))
    conf = payload.get("confidence") or {}
    lines.append("")
    lines.append("【置信度 %.2f（%s）】%s" % (
        conf.get("score", 0), conf.get("level", ""), "；".join(conf.get("reasons", [])[:2])))
    lines.append("")
    lines.append("【風險聲明】%s" % (payload.get("guard", {}).get("note", "")))
    return "\n".join(lines)


# ---------------------------------------------------------------- 大模型層

def _facts(payload: dict) -> str:
    clusters = "\n".join(
        "- %s｜%s｜強度 %.2f｜%d 條｜來源 %s" % (
            c["label"], c["direction"], c["strength"], c["count"],
            "、".join(c["publishers"]) or "A2")
        for c in payload.get("clusters", [])
    ) or "（無）"
    evidence = "\n".join(
        "- %s｜%s｜%s｜%s｜%s｜信心 %.2f" % (
            e["id"], e["agent"], e["title"], e.get("publisher", ""), e.get("sentiment", ""),
            e.get("confidence") or 0)
        for e in payload.get("evidence", [])[:16]
    ) or "（無）"
    conflicts = "\n".join(
        "- %s｜A1 %s｜A2 %s" % (c["topic"], c["a1"], c["a2"]) for c in payload.get("conflicts", [])
    ) or "（無）"
    transmission = "\n".join(
        "- %s → %s → %s（%s，%s）證據 %s" % (
            t["from"], t["channel"], "／".join(t["to"]), t["direction"], t["strength"],
            ",".join(t["evidence"]))
        for t in payload.get("transmission", [])
    ) or "（無）"
    conf = payload.get("confidence") or {}
    quality = payload.get("data_quality") or {}
    return {
        "question": (payload.get("question") or "生成今天的晨報")
                    + ("｜使用者關注：" + payload["focus"] if payload.get("focus") else ""),
        "clusters": clusters,
        "evidence": evidence,
        "conflicts": conflicts,
        "transmission": transmission,
        "confidence": "%.2f（%s）｜%s" % (conf.get("score", 0), conf.get("level", ""),
                                         "；".join(conf.get("reasons", []))),
        "quality": "獨立媒體 %d 個；單一來源主題 %s；最新消息 %.1f 小時前" % (
            quality.get("distinct_publishers", 0),
            "、".join(quality.get("single_source_topics", [])) or "無",
            quality.get("newest_age_hours") or -1),
        "draft": _draft_text(payload),
    }


def _draft_text(payload: dict) -> str:
    """規則層初稿。給模型「改寫」而不是「從零寫」，這是控制幻覺與延遲的關鍵。"""
    s = payload.get("sections") or {}
    lines = ["【發生什麼】"]
    for i, item in enumerate(s.get("what_happened", []), 1):
        lines.append("%d. %s" % (i, item))
    lines.append("【意味什麼】")
    for i, item in enumerate(s.get("so_what", []), 1):
        lines.append("%d. %s" % (i, item))
    lines.append("【盯什麼】")
    for w in s.get("watch", []):
        lines.append("- %s：%s；觸發 %s；作廢 %s" % (
            w.get("item", ""), w.get("why", ""), w.get("trigger", ""), w.get("invalidates", "")))
    return "\n".join(lines)


def _validate_llm(result: dict) -> dict:
    """只驗證 headline 與 brief。

    為什麼只要這兩個：大模型寫 1000+ tokens 要 60 秒，而結構化章節（what_happened / so_what /
    watch）由規則層產出本來就沒有幻覺、也不需要文采。把模型的輸出縮到「主線句 + 正文」，
    延遲由 65 秒降到 25 秒左右，而且結構永遠不會因模型抽風而壞掉。
    """
    if not isinstance(result, dict):
        raise brains.LLMError("模型輸出不是物件")
    headline = str(result.get("headline") or "").strip()
    brief = str(result.get("brief") or "").strip()
    if len(headline) < 6:
        raise brains.LLMError("主線句缺失")
    if len(headline) > 60:
        raise brains.LLMError("主線句過長（%d 字）" % len(headline))
    if len(brief) < 150:
        raise brains.LLMError("正文過短（%d 字）" % len(brief))
    return {"headline": headline, "brief": brief}


def _citations(text: str) -> int:
    import re
    return len(re.findall(r"\b[NP]\d{1,2}\b", text or ""))


# ---------------------------------------------------------------- 意圖識別

GREETINGS = {
    "hello", "hi", "hey", "yo", "hihi", "hiya", "good morning", "morning",
    "你好", "妳好", "您好", "哈囉", "哈罗", "嗨", "在嗎", "在吗", "有人嗎",
    "早晨", "早上好", "午安", "安安", "師傅", "大佬",
}
HELP_WORDS = {
    "help", "你能做什麼", "你能做什么", "你可以做什麼", "你可以做什么",
    "你會做什麼", "你会做什么", "你會什麼", "你会什么", "有什麼功能", "有什么功能",
    "怎麼用", "怎么用", "功能", "介紹一下", "介绍一下", "介紹自己", "介绍自己",
    "你是誰", "你是谁", "用法", "說明", "说明", "可以問什麼", "可以问什么",
}
TEST_WORDS = {"test", "testing", "測試", "测试", "ping", "1", "123", "？", "?", "在不在"}


def classify_intent(text: str) -> str:
    """判斷使用者這句話是「打招呼 / 問功能 / 測系統」還是「要晨報」。

    為什麼要有這層：A5 是唯一面向使用者的入口，輸入框長得像對話框。
    使用者打 hello，期待的是回一句話，不是被塞一份晨報——那就是答非所問。
    """
    raw = (text or "").strip().lower()
    if not raw:
        return "brief"
    core = raw.strip("!！?？。.,，~～ ")
    if core in GREETINGS or raw in GREETINGS:
        return "greeting"
    if core in TEST_WORDS:
        return "test"
    for w in HELP_WORDS:
        if w in raw:
            return "help"
    return "brief"


def _last_brief() -> dict | None:
    for row in ledger.list_runs(12):
        if row.get("kind") == "brief":
            return row
    return None


def status_context(upstream: dict) -> str:
    """給模型的事實：連線狀態與時間。不讓它自己猜。"""
    a1 = "已連線" if upstream["a1"].get("ok") else "未連線"
    a2 = "已連線" if upstream["a2"].get("ok") else "未連線"
    return ("【系統狀態，事實】現在 %s（香港時間）；A1 新聞 %s；A2 政要 %s；"
            "A3 執行 未實作；A4 風控 未接入（本服務只有建議權）。"
            % (_now_iso()[11:16], a1, a2))


def chat_payload(question: str, intent: str, reply: str | None = None,
                 upstream: dict | None = None) -> dict:
    """不是要晨報時的回覆。不呼叫大模型：招呼和功能說明應該秒回、零成本。"""
    now = _now_iso()[11:16]
    last = _last_brief()
    upstream = upstream or connectors.health()
    a1 = "已連線" if upstream["a1"].get("ok") else "未連線"
    a2 = "已連線" if upstream["a2"].get("ok") else "未連線"

    if reply:
        pass
    elif intent == "greeting":
        reply = "你好，我是 A5 指揮官。\n\n我負責把 A1 新聞與 A2 政要的結果匯總成一份晨報，" \
                "做交叉驗證、標出分歧、給出可解釋的置信度，並在合規護欄下交付。"
    elif intent == "help":
        reply = (
            "我能做四件事：\n"
            "1. 生成晨報：匯總 A1／A2，做二次分析與情境推演\n"
            "2. 單篇探針：貼一段新聞，出傳導鏈與受影響標的\n"
            "3. 追問：對某一份晨報繼續問，答案必須帶證據 ID\n"
            "4. 比較：任選兩份晨報，看主題與方向變了什麼\n\n"
            "限制：A3 執行與 A4 風控還沒接入，所以我只給研究輸出，不產生下單指令。"
        )
    else:  # test
        llm = "已啟用（%s）" % settings.brain_shared_model if settings.llm_enabled else "未設定 key，走規則引擎"
        reply = "系統正常。大模型：%s；A1 %s、A2 %s；風控閘門 advisory_only。" % (llm, a1, a2)

    tail = "現在 %s（香港時間）。A1 %s、A2 %s。" % (now, a1, a2)
    if last:
        tail += "最近一份晨報是 %s，置信度 %.2f。" % (
            (last.get("created_at") or "")[11:16], last.get("confidence") or 0)

    return {
        "kind": "chat",
        "intent": intent,
        "question": question,
        "reply": reply,
        "context": tail,
        "suggestions": ["生成今天的晨報", "看最近一份晨報", "你能做什麼？", "分析一篇新聞"],
        "brain": {"topology": "rule", "label": "對話（規則）", "mode": "rule",
                  "prompt_variant": settings.prompt_variant, "models": {},
                  "degraded": False, "degrade_reason": ""},
        "guard": {"passed": True, "blocked": [], "broken_evidence": [],
                  "a4_gate": "advisory_only", "a4_connected": False, "advisory": True,
                  "note": "A4 風控未接入：A5 只有建議權。"},
        "disclaimer": "PrismQuant 為研究與決策輔助工具，不構成投資建議。",
        "stats": {"latency_ms": 0, "tokens": 0},
        "generated_at": _now_iso(),
    }


# ---------------------------------------------------------------- 主流程

def run(question: str = "", topology: str | None = None, progress=None) -> dict:
    started = time.time()
    q = (question or "").strip() or "生成今天的晨報"

    def emit(stage: str, message: str, **extra):
        if progress:
            progress({"stage": stage, "message": message,
                      "elapsed_ms": int((time.time() - started) * 1000), **extra})

    # 先讓模型自己判斷；沒有 key 或失敗才退回關鍵詞規則
    emit("think", "判斷你想做什麼")
    upstream = connectors.health()
    routed = None if q == "生成今天的晨報" else brains.route(q, status_context(upstream))
    focus = ""

    if routed:
        emit("think", "判斷完成：%s" % ("要晨報" if routed["intent"] == "brief" else "先回話"),
             done=True)
        if routed["intent"] == "chat":
            payload = chat_payload(q, "chat", reply=routed["reply"], upstream=upstream)
            payload["brain"].update({"mode": "llm", "label": "對話（LLM）",
                                     "llm_meta": routed.get("meta") or {}})
            payload["stats"]["tokens"] = ((routed.get("meta") or {}).get("completion_tokens") or 0)
            payload["stats"]["latency_ms"] = int((time.time() - started) * 1000)
            payload["run_id"] = ledger.new_id("c")
            ledger.save(payload, snapshot={"question": q, "intent": "chat", "route": routed})
            emit("done", "已回覆", run_id=payload["run_id"])
            return payload
        focus = routed.get("focus") or ""
    else:
        intent = classify_intent(q)
        emit("think", "無大模型，用規則判斷：%s" % intent, done=True)
        if intent != "brief":
            payload = chat_payload(q, intent, upstream=upstream)
            payload["stats"]["latency_ms"] = int((time.time() - started) * 1000)
            payload["run_id"] = ledger.new_id("c")
            ledger.save(payload, snapshot={"question": q, "intent": intent})
            emit("done", "已回覆（%s）" % intent, run_id=payload["run_id"])
            return payload

    emit("upstream", "呼叫 A1 新聞與 A2 政要監控")
    (a1, a1_meta), (a2, a2_meta) = connectors.fetch_all()
    a1_online = bool(a1_meta.get("ok"))
    a2_online = bool(a2_meta.get("ok"))
    emit("upstream",
         "A1 %s（%d 條）／A2 %s（%d 條）" % (
             "已連線" if a1_online else "未連線", len(a1),
             "已連線" if a2_online else "未連線", len(a2)),
         done=True, upstream={"a1": a1_online, "a2": a2_online})
    if a1_online and a2_online:
        # 在線但沒有資料 ≠ 離線：前者要老實留空，後者才用示例
        sample_mode = bool(a1_meta.get("sampled") or a2_meta.get("sampled"))
        empty = [n for n, m in (("A1 新聞", a1_meta), ("A2 政要", a2_meta)) if m.get("empty")]
        source_note = ("%s 已連線但沒有資料（可能未執行 /collect），本頁只覆蓋有資料的部分。"
                       % "、".join(empty)) if empty else ""
    else:
        bundle = connectors.sample_bundle()
        a1 = bundle["a1"]["events"]
        a2 = bundle["a2"]["events"]
        sample_mode = True
        failed = [name for name, meta in (("A1 新聞", a1_meta), ("A2 政要", a2_meta)) if not meta.get("ok")]
        source_note = ("上游 %s 未連線，資料改用內建示例（分析流程是真實執行的，只有輸入是示例）。"
                       % "、".join(failed))

    upstream_meta = {
        "a1": {**a1_meta, "name": "A1 新聞情報"},
        "a2": {**a2_meta, "name": "A2 政要監控"},
        "sample_mode": sample_mode,
        "note": source_note,
    }

    payload = analyze.build_brief(q, a1, a2, sample_mode, store.get_watchlist(),
                                  upstream_meta, focus=focus)
    payload["focus"] = focus
    emit("analyze",
         "L0 正規化 → L1 交叉驗證（%d 個主題 / %d 組分歧）→ L2 情境推演" % (
             len(payload.get("clusters", [])), len(payload.get("conflicts", []))),
         done=True)

    brain_plan = brains.plan(topology or settings.brain_topology)
    mode = "rule"
    degrade = ""
    llm_meta: dict = {}

    if brain_plan.topology != "rule" and settings.llm_api_key:
        emit("llm", "大模型（%s）改寫中，約 30–60 秒" % (brain_plan.role("a5").model), started=True)
        try:
            role = brain_plan.role("a5")
            system = brains.load_prompt("brief_system") or "你是 PrismQuant 的指揮官 A5。"
            template = brains.load_prompt("brief_user")
            user = template.format(**_facts(payload))
            result, llm_meta = brains.chat_json(system, user, role,
                                                max_tokens=settings.llm_max_tokens)
            good = _validate_llm(result)
            payload["headline"] = good["headline"]
            payload["brief_text"] = good["brief"]
            payload["reading_minutes"] = analyze.reading_minutes(payload["sections"], good["brief"])
            mode = "llm"
            emit("llm", "完成（%d 字）" % len(good["brief"]),
                 done=True, tokens=(llm_meta.get("completion_tokens") or 0))
        except brains.LLMError as exc:
            degrade = "大模型層降級為規則層：%s" % str(exc)[:160]
            emit("llm", "降級：%s" % str(exc)[:80], done=True)
    elif brain_plan.topology != "rule" and not settings.llm_api_key:
        degrade = "未設定 PQ_LLM_API_KEY，已自動降級為規則層（不影響其餘功能）。"

    payload["brain"] = {
        "topology": brain_plan.topology,
        "label": brain_plan.label,
        "mode": mode,
        "prompt_variant": settings.prompt_variant,
        "models": {k: v.model for k, v in brain_plan.roles.items()},
        "degraded": bool(degrade),
        "degrade_reason": degrade,
        "llm_meta": llm_meta,
    }
    payload["source_note"] = source_note
    payload["coverage"] = coverage(upstream_meta, sample_mode)
    payload["kind"] = "brief"

    if not payload.get("brief_text"):
        payload["brief_text"] = compose_text(payload)

    cited = _citations(payload["brief_text"]) + sum(_citations(x) for x in payload["sections"]["what_happened"])
    payload["quality_flags"] = {
        "citation_count": cited,
        "citation_ok": cited >= max(2, len(payload["sections"]["what_happened"]) // 2),
        "reading_minutes": payload["reading_minutes"],
        "over_target": payload["reading_minutes"] > settings.brief_target_max_minutes,
    }

    emit("guard", "合規檢查：禁語掃描、證據可追溯、A4 閘門")
    payload = guardrails.apply(payload)

    prompt_tokens = llm_meta.get("prompt_tokens") or 0
    completion_tokens = llm_meta.get("completion_tokens") or 0
    cost_usd = (prompt_tokens * settings.price_in_per_m
                + completion_tokens * settings.price_out_per_m) / 1_000_000
    payload["usage"] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost_usd": round(cost_usd, 6),
        "priced": settings.price_in_per_m > 0 or settings.price_out_per_m > 0,
        "note": "估算：按 .env 的 PQ_PRICE_IN_PER_M / PQ_PRICE_OUT_PER_M 單價計算",
    }
    payload["stats"] = {
        "latency_ms": int((time.time() - started) * 1000),
        "upstream_ms": {k: (upstream_meta[k].get("latency_ms") or 0) for k in ("a1", "a2")},
        "tokens": prompt_tokens + completion_tokens,
    }
    payload["generated_at"] = _now_iso()

    ledger.save(payload, snapshot={
        "question": q, "topology": brain_plan.topology, "a1": a1, "a2": a2,
        "sample_mode": sample_mode,
    })
    emit("done", "完成", run_id=payload["run_id"],
         latency_ms=payload["stats"]["latency_ms"])
    return payload


# ---------------------------------------------------------------- 追問（多輪）

ASK_SYSTEM = (
    "你是 PrismQuant 的指揮官 A5。使用者正在追問你剛剛產出的晨報。\n"
    "規則：\n"
    "1. 只能使用下方的事實與證據回答。沒有資料就直說「晨報裡沒有這個資訊」，不要編。\n"
    "2. 回答要引用證據 ID（N1 / P2 這種），每條結論至少一個。\n"
    "3. 繁體中文，300 字內，直接回答，不要重述整份晨報。\n"
    "4. 不准給保證性表述，不准輸出下單指令。\n"
    "輸出 JSON：{\"answer\":\"…\",\"cited\":[\"N1\",\"P3\"],\"limits\":\"這答案的局限一句話\"}"
)


def _ask_facts(payload: dict) -> str:
    ev = "\n".join(
        "- %s｜%s｜%s｜%s｜%s" % (e["id"], e["agent"], e["title"], e.get("publisher", ""),
                                 e.get("sentiment", ""))
        for e in payload.get("evidence", [])
    )
    cf = "\n".join("- %s｜A1 %s｜A2 %s" % (c["topic"], c["a1"], c["a2"])
                   for c in payload.get("conflicts", [])) or "（無）"
    return (
        "晨報主線：%s\n\n主題：\n%s\n\n證據：\n%s\n\n分歧：\n%s\n\n置信度：%.2f（%s）" % (
            payload.get("headline", ""),
            "\n".join("- %s｜%s｜強度 %.2f" % (c["label"], c["direction"], c["strength"])
                      for c in payload.get("clusters", [])),
            ev, cf,
            (payload.get("confidence") or {}).get("score", 0),
            (payload.get("confidence") or {}).get("level", ""),
        )
    )


def ask(payload: dict, question: str) -> dict:
    """對某一份晨報追問。沒有 key 時退化為「證據檢索」，並誠實說明限制。"""
    q = (question or "").strip()
    if not q:
        raise ValueError("問題不能為空")
    started = time.time()
    facts = _ask_facts(payload)
    plan = brains.plan(settings.brain_topology)
    role = plan.role("a5") or brains.Role("A5", settings.brain_shared_model, 0.2, "")

    if plan.topology != "rule" and settings.llm_api_key:
        try:
            out, meta = brains.chat_json(
                ASK_SYSTEM,
                "%s\n\n使用者追問：%s" % (facts, q),
                role, max_tokens=500,
            )
            answer = str(out.get("answer") or "").strip()
            if not answer:
                raise brains.LLMError("模型沒有給出答案")
            cited = [c for c in (out.get("cited") or []) if isinstance(c, str)]
            result = {
                "kind": "answer",
                "question": q,
                "answer": answer,
                "cited": cited,
                "limits": str(out.get("limits") or ""),
                "mode": "llm",
                "usage": {
                    "prompt_tokens": meta.get("prompt_tokens") or 0,
                    "completion_tokens": meta.get("completion_tokens") or 0,
                    "cost_usd": round(((meta.get("prompt_tokens") or 0) * settings.price_in_per_m
                                       + (meta.get("completion_tokens") or 0) * settings.price_out_per_m)
                                      / 1_000_000, 6),
                },
                "brain": {"topology": plan.topology, "label": plan.label, "mode": "llm",
                          "model": role.model, "degraded": False},
                "elapsed_ms": int((time.time() - started) * 1000),
            }
        except brains.LLMError as exc:
            result = _ask_retrieval(payload, q, "大模型不可用（%s），以下是證據檢索結果。" % str(exc)[:80])
            result["elapsed_ms"] = int((time.time() - started) * 1000)
    else:
        result = _ask_retrieval(payload, q, "目前是規則模式，只能做證據檢索，不能推理。設定 PQ_LLM_API_KEY 後才是真正的對話。")
        result["elapsed_ms"] = int((time.time() - started) * 1000)

    result["parent_run_id"] = payload.get("run_id")
    result["run_id"] = ledger.new_id("a")
    ledger.save(result, snapshot={"parent": payload.get("run_id"), "question": q})
    return result


def _ask_retrieval(payload: dict, q: str, note: str) -> dict:
    """沒有大模型時的退路：按關鍵詞在證據裡找，不假裝在推理。"""
    import re
    tokens = [t for t in re.findall(r"[\u4e00-\u9fff]{2,4}|[A-Za-z]{2,}", q)]
    scored = []
    for e in payload.get("evidence", []):
        text = "%s %s %s" % (e.get("title", ""), e.get("summary", ""), e.get("topic", ""))
        hits = sum(1 for t in tokens if t in text)
        if hits:
            scored.append((hits, e))
    scored.sort(key=lambda x: -x[0])
    top = [e for _, e in scored[:4]]
    if top:
        lines = ["與「%s」最相關的證據：" % q]
        for e in top:
            lines.append("· %s（%s，%s）%s" % (e["id"], e["agent"], e.get("publisher", ""), e["title"]))
        answer = "\n".join(lines)
        cited = [e["id"] for e in top]
    else:
        answer = "晨報的證據裡沒有直接對應「%s」的內容。" % q
        cited = []
    return {
        "kind": "answer", "question": q, "answer": answer, "cited": cited,
        "limits": note, "mode": "retrieval",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
        "brain": {"topology": "rule", "label": "證據檢索", "mode": "rule", "model": "", "degraded": True},
    }


def probe(title: str, text: str, source: str = "", url: str = "") -> dict:
    started = time.time()
    (a1, a1_meta), (a2, a2_meta) = connectors.fetch_all(a1_limit=40, a2_limit=20)
    if not (a1_meta.get("ok") and a2_meta.get("ok")):
        bundle = connectors.sample_bundle()
        a1, a2 = bundle["a1"]["events"], bundle["a2"]["events"]

    existing = analyze.normalize_a1(a1) + analyze.normalize_a2(a2)
    analyze.assign_ids(existing)

    result = probe_mod.analyze(title, text, source, url, existing, store.get_watchlist())
    result["brain"] = {"topology": "rule", "label": "規則探針", "mode": "rule",
                       "prompt_variant": settings.prompt_variant, "models": {},
                       "degraded": False, "degrade_reason": ""}
    result["stats"] = {"latency_ms": int((time.time() - started) * 1000)}
    result["generated_at"] = _now_iso()
    result = guardrails.apply(result)
    ledger.save(result, snapshot={"title": title, "text": text[:2000], "source": source, "url": url})
    return result


# ---------------------------------------------------------------- A4 模擬閘門

def simulate_gate(payload: dict) -> dict:
    """把傳導鏈當成「假設的執行意圖」，跑一遍風控規則。

    這是模擬，不是風控：A4 還沒有人實作。但它回答了一個真問題——
    如果 A3 真的照這份晨報發單，風控會放行、減半、還是直接否決？
    規則全部寫在這裡，可被逐條檢查，不呼叫大模型。
    """
    rows = (payload.get("transmission") or [])[:4]
    conflicts = {c.get("topic") for c in payload.get("conflicts") or []}
    single = set((payload.get("data_quality") or {}).get("single_source_topics") or [])
    conf = (payload.get("confidence") or {}).get("score", 0) or 0

    intents = []
    total = 0.0
    for t in rows:
        label = t.get("from", "")
        direction = t.get("direction")
        strength = t.get("strength")
        base = 10.0 if strength == "強" else (8.0 if strength == "中" else 5.0)
        size = base
        verdict = "pass"
        reasons = []

        if direction == "中性":
            verdict, size = "reject", 0.0
            reasons.append("方向中性：沒有可交易的方向，A4 不放行")
        elif label in conflicts:
            verdict, size = "reject", 0.0
            reasons.append("A1／A2 在「%s」上仍分歧，等正式文件落地再談" % label)
        else:
            if label in single:
                verdict = "degrade"
                size = round(base * 0.5, 1)
                reasons.append("單一來源：倉位減半，等第二個媒體確認")
            if conf < 0.5:
                verdict = "degrade"
                size = min(size, 3.0)
                reasons.append("晨報置信度 %.2f 低於 0.50：只允許觀察倉" % conf)
            if not reasons:
                reasons.append("方向明確、已交叉驗證、無未解分歧")

        total += size
        intents.append({
            "instrument": (t.get("to") or ["—"])[0],
            "label": label,
            "direction": direction,
            "size_pct": size,
            "verdict": verdict,
            "reasons": reasons,
            "evidence": t.get("evidence", []),
        })

    scaled = False
    if total > 30:
        scale = 30.0 / total
        for it in intents:
            it["size_pct"] = round(it["size_pct"] * scale, 1)
        total = 30.0
        scaled = True

    passed = sum(1 for i in intents if i["verdict"] == "pass")
    degraded = sum(1 for i in intents if i["verdict"] == "degrade")
    rejected = sum(1 for i in intents if i["verdict"] == "reject")

    return {
        "simulated": True,
        "gate": "A4 mock",
        "base_run_id": payload.get("run_id"),
        "headline": payload.get("headline"),
        "intents": intents,
        "summary": {
            "intents": len(intents),
            "pass": passed,
            "degrade": degraded,
            "reject": rejected,
            "notional_pct": round(total, 1),
            "capped_at_30": scaled,
        },
        "rules": [
            "方向中性 → reject",
            "A1／A2 未解分歧 → reject",
            "單一來源 → 倉位減半",
            "晨報置信度 < 0.50 → 只允許觀察倉（≤3%）",
            "同方向合計 > 30% → 按比例縮回 30%",
        ],
        "note": "模擬輸出：A4 未接入，這裡只示範風控會如何否決，不產生任何下單指令。",
    }


# ---------------------------------------------------------------- 五代理覆蓋範圍

AGENT_META = {
    "A1": {"name": "新聞情報", "owner": "隊友", "role": "感知",
           "needs": "多來源 RSS（多於一家）、把媒體名寫進 source 欄位（現在只有 \"RSS\"）"},
    "A2": {"name": "政要監控", "owner": "隊友", "role": "感知",
           "needs": "/api/v1/analyses 回傳 direction、entities、markets、confidence"},
    "A3": {"name": "執行代理", "owner": "隊友", "role": "決策",
           "needs": "GET /api/v1/intents：instrument、direction、size_range、logic、evidence_ids"},
    "A4": {"name": "風控代理", "owner": "隊友", "role": "閘門",
           "needs": "GET /api/v1/risk/check：verdict（pass/degrade/reject）、limits、reasons"},
    "A5": {"name": "指揮官", "owner": "本 repo", "role": "編排",
           "needs": "已交付：匯總、二次分析、證據鏈、合規、交付"},
}


def coverage(upstream_meta: dict, sample_mode: bool) -> dict:
    """A5 必須講清楚「這一頁用了誰的資料、缺誰」。

    缺的部分不是用假數據填滿，而是留空並寫明等誰——這是隊友接手整合時最小摩擦的做法。
    """
    out = []
    for aid in ("A1", "A2"):
        m = upstream_meta.get(aid.lower()) or {}
        if not m.get("ok"):
            status, present, note = "offline", False, "未連線，本頁改用示例資料"
        elif m.get("empty"):
            status, present, note = "empty", False, "已連線但沒有資料（未執行 /collect？）"
        else:
            status, present, note = "ok", True, "已接入 %d 條" % (m.get("count") or 0)
        out.append({"id": aid, **AGENT_META[aid], "status": status,
                    "present": present, "note": note})

    out.append({"id": "A3", **AGENT_META["A3"], "status": "missing", "present": False,
                "note": "未實作：本頁不產生任何標的建議或下單意圖，相關區塊留空"})
    out.append({"id": "A4", **AGENT_META["A4"], "status": "missing", "present": False,
                "note": "未接入：A5 只有建議權（advisory_only），沒有風控裁決"})
    out.append({"id": "A5", **AGENT_META["A5"], "status": "ok", "present": True,
                "note": "本服務"})

    return {
        "agents": out,
        "missing": [a["id"] for a in out if not a["present"]],
        "sample_mode": sample_mode,
        "summary": "已接入：%s｜缺席：%s" % (
            "、".join(a["id"] for a in out if a["present"]) or "無",
            "、".join(a["id"] for a in out if not a["present"]) or "無"),
    }


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now(analyze.HK_TZ).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 與上一份的差異

def _diff_payloads(cur: dict, prev: dict) -> dict:
    """比較兩份晨報 payload 的結構化差異（不含 ledger 元資料）。"""
    cur_map = {c["label"]: c for c in cur.get("clusters", [])}
    prev_map = {c["label"]: c for c in prev.get("clusters", [])}
    added = [k for k in cur_map if k not in prev_map]
    dropped = [k for k in prev_map if k not in cur_map]
    flipped = [{"topic": k, "from": prev_map[k]["direction"], "to": cur_map[k]["direction"]}
               for k in cur_map if k in prev_map and cur_map[k]["direction"] != prev_map[k]["direction"]]

    cur_ids = {e["id"] for e in cur.get("evidence", [])}
    prev_ids = {e["id"] for e in prev.get("evidence", [])}
    cur_conf = (cur.get("confidence") or {}).get("score", 0)
    prev_conf = (prev.get("confidence") or {}).get("score", 0)

    return {
        "headline": {"from": prev.get("headline"), "to": cur.get("headline"),
                     "changed": prev.get("headline") != cur.get("headline")},
        "confidence": {"from": prev_conf, "to": cur_conf, "delta": round(cur_conf - prev_conf, 3)},
        "added_topics": added,
        "dropped_topics": dropped,
        "direction_flips": flipped,
        "new_evidence": sorted(cur_ids - prev_ids),
        "gone_evidence": sorted(prev_ids - cur_ids),
        "summary": _diff_sentence(added, dropped, flipped, cur_conf - prev_conf),
    }


def diff_with_previous(run_id: str) -> dict:
    """「今天比上一份多了什麼」——這是每日晨報真正的價值，也是 ledger 的用途。"""
    rows = ledger.list_runs(80)
    idx = next((i for i, r in enumerate(rows) if r["run_id"] == run_id), None)
    if idx is None:
        raise KeyError("run_not_found")
    prev_row = next((r for r in rows[idx + 1:] if r["kind"] == "brief"), None)
    cur_row = ledger.get(run_id)
    if cur_row is None:
        raise KeyError("run_not_found")
    if prev_row is None:
        return {"has_previous": False, "note": "這是第一份晨報，沒有可比對的上一份。"}

    out = _diff_payloads(cur_row["payload"], ledger.get(prev_row["run_id"])["payload"])
    out.update({"has_previous": True,
                "previous_run_id": prev_row["run_id"],
                "previous_at": prev_row["created_at"]})
    return out


def compare_runs(base_run_id: str, target_run_id: str) -> dict:
    """任選兩份晨報比較。歷史記錄頁的「選兩份比較」用它。"""
    a = ledger.get(base_run_id)
    b = ledger.get(target_run_id)
    if a is None or b is None:
        raise KeyError("run_not_found")
    if a["kind"] != "brief" or b["kind"] != "brief":
        raise ValueError("只支援比較兩份晨報")
    out = _diff_payloads(b["payload"], a["payload"])
    out.update({
        "has_previous": True,
        "mode": "pair",
        "base": {"run_id": base_run_id, "at": a["created_at"],
                 "headline": a["payload"].get("headline")},
        "target": {"run_id": target_run_id, "at": b["created_at"],
                   "headline": b["payload"].get("headline")},
    })
    return out


def _diff_sentence(added, dropped, flipped, delta) -> str:
    parts = []
    if added:
        parts.append("新增 %s" % "、".join(added[:3]))
    if dropped:
        parts.append("掉出 %s" % "、".join(dropped[:3]))
    if flipped:
        parts.append("%s 方向翻轉" % "、".join(f["topic"] for f in flipped[:2]))
    if abs(delta) >= 0.03:
        parts.append("置信度 %s %.2f" % ("上升" if delta > 0 else "下降", abs(delta)))
    return "；".join(parts) if parts else "與上一份相同：沒有新主題、沒有方向翻轉，今天不需要改倉位。"
