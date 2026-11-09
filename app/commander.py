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
        "question": payload.get("question") or "生成今天的晨報",
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


# ---------------------------------------------------------------- 主流程

def run(question: str = "", topology: str | None = None, progress=None) -> dict:
    started = time.time()
    q = (question or "").strip() or "生成今天的晨報"

    def emit(stage: str, message: str, **extra):
        if progress:
            progress({"stage": stage, "message": message,
                      "elapsed_ms": int((time.time() - started) * 1000), **extra})

    emit("upstream", "呼叫 A1 新聞與 A2 政要監控")
    (a1, a1_meta), (a2, a2_meta) = connectors.fetch_all()
    upstream_ok = bool(a1_meta.get("ok")) and bool(a2_meta.get("ok"))
    emit("upstream",
         "A1 %s（%d 條）／A2 %s（%d 條）" % (
             "已連線" if a1_meta.get("ok") else "未連線", len(a1),
             "已連線" if a2_meta.get("ok") else "未連線", len(a2)),
         done=True, upstream={"a1": bool(a1_meta.get("ok")), "a2": bool(a2_meta.get("ok"))})
    if upstream_ok:
        sample_mode = bool(a1_meta.get("sampled") or a2_meta.get("sampled"))
        source_note = ""
    else:
        bundle = connectors.sample_bundle()
        a1 = bundle["a1"]["events"]
        a2 = bundle["a2"]["events"]
        sample_mode = True
        failed = [name for name, meta in (("A1 新聞", a1_meta), ("A2 政要", a2_meta)) if not meta.get("ok")]
        source_note = "上游 %s 未連線，整份晨報改用內建示例資料（不真假混用）。" % "、".join(failed)

    upstream_meta = {
        "a1": {**a1_meta, "name": "A1 新聞情報"},
        "a2": {**a2_meta, "name": "A2 政要監控"},
        "sample_mode": sample_mode,
        "note": source_note,
    }

    payload = analyze.build_brief(q, a1, a2, sample_mode, store.get_watchlist(), upstream_meta)
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


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now(analyze.HK_TZ).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 與上一份的差異

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

    prev = ledger.get(prev_row["run_id"])["payload"]
    cur = cur_row["payload"]

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
        "has_previous": True,
        "previous_run_id": prev_row["run_id"],
        "previous_at": prev_row["created_at"],
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
