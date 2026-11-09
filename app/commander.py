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

from app import analyze, brains, connectors, guardrails, ledger, probe as probe_mod
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
    }


def _validate_llm(result: dict) -> dict:
    if not isinstance(result, dict):
        raise brains.LLMError("模型輸出不是物件")
    headline = str(result.get("headline") or "").strip()
    happened = [str(x).strip() for x in (result.get("what_happened") or []) if str(x).strip()]
    so_what = [str(x).strip() for x in (result.get("so_what") or []) if str(x).strip()]
    watch = []
    for w in result.get("watch") or []:
        if isinstance(w, dict) and w.get("item"):
            watch.append({
                "item": str(w.get("item", "")), "why": str(w.get("why", "")),
                "trigger": str(w.get("trigger", "")), "invalidates": str(w.get("invalidates", "")),
            })
    brief = str(result.get("brief") or "").strip()
    if not headline or len(happened) < 2 or len(so_what) < 2 or len(brief) < 80:
        raise brains.LLMError("模型輸出不符合契約（headline/sections/brief 不完整）")
    return {"headline": headline, "what_happened": happened[:5], "so_what": so_what[:5],
            "watch": watch[:5], "brief": brief}


def _citations(text: str) -> int:
    import re
    return len(re.findall(r"\b[NP]\d{1,2}\b", text or ""))


# ---------------------------------------------------------------- 主流程

def run(question: str = "", topology: str | None = None) -> dict:
    started = time.time()
    q = (question or "").strip() or "生成今天的晨報"

    (a1, a1_meta), (a2, a2_meta) = connectors.fetch_all()
    upstream_ok = bool(a1_meta.get("ok")) and bool(a2_meta.get("ok"))
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

    payload = analyze.build_brief(q, a1, a2, sample_mode, settings.watchlist, upstream_meta)

    brain_plan = brains.plan(topology or settings.brain_topology)
    mode = "rule"
    degrade = ""
    llm_meta: dict = {}

    if brain_plan.topology != "rule" and settings.llm_api_key:
        try:
            role = brain_plan.role("a5")
            system = brains.load_prompt("brief_system") or "你是 PrismQuant 的指揮官 A5。"
            template = brains.load_prompt("brief_user")
            user = template.format(**_facts(payload))
            result, llm_meta = brains.chat_json(system, user, role)
            good = _validate_llm(result)
            payload["headline"] = good["headline"]
            payload["sections"] = {
                "what_happened": good["what_happened"],
                "so_what": good["so_what"],
                "watch": good["watch"] or payload["sections"]["watch"],
            }
            payload["brief_text"] = good["brief"]
            payload["reading_minutes"] = analyze.reading_minutes(payload["sections"], good["brief"])
            mode = "llm"
        except brains.LLMError as exc:
            degrade = "大模型層降級為規則層：%s" % str(exc)[:160]
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

    payload = guardrails.apply(payload)
    payload["stats"] = {
        "latency_ms": int((time.time() - started) * 1000),
        "upstream_ms": {k: (upstream_meta[k].get("latency_ms") or 0) for k in ("a1", "a2")},
        "tokens": (llm_meta.get("prompt_tokens") or 0) + (llm_meta.get("completion_tokens") or 0),
    }
    payload["generated_at"] = _now_iso()

    ledger.save(payload, snapshot={
        "question": q, "topology": brain_plan.topology, "a1": a1, "a2": a2,
        "sample_mode": sample_mode,
    })
    return payload


def probe(title: str, text: str, source: str = "", url: str = "") -> dict:
    started = time.time()
    (a1, a1_meta), (a2, a2_meta) = connectors.fetch_all(a1_limit=40, a2_limit=20)
    if not (a1_meta.get("ok") and a2_meta.get("ok")):
        bundle = connectors.sample_bundle()
        a1, a2 = bundle["a1"]["events"], bundle["a2"]["events"]

    existing = analyze.normalize_a1(a1) + analyze.normalize_a2(a2)
    analyze.assign_ids(existing)

    result = probe_mod.analyze(title, text, source, url, existing, settings.watchlist)
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
