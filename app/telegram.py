# -*- coding: utf-8 -*-
"""Telegram 交付通道。

隊友說「去 telegram 頻道」，而 A5 本來就是唯一面向使用者的出口，所以推送放在 A5 是合理的。

兩種模式：
- 有 PQ_TELEGRAM_BOT_TOKEN + PQ_TELEGRAM_CHAT_ID：真的發送。
- 沒有：dry-run，回傳「即將送出的全文」，前端可以照著手機氣泡預覽。
  這不是假裝發送，是把 payload 誠實地展示出來，方便示範與驗收。

Webhook 指令（需要 PQ_TELEGRAM_WEBHOOK_SECRET，未設定則只允許本機呼叫）：
    /brief [問題]   產生晨報並回覆
    /status         上游與大腦狀態
    /probe <新聞>   單篇探針
    /help           指令表
"""
from __future__ import annotations

import httpx

from app import brains, commander, connectors, ledger
from app.config import settings

MAX_LEN = 3900


def build_message(payload: dict) -> str:
    if payload.get("kind") == "probe":
        lines = [
            "🔎 PrismQuant · 單篇探針",
            "",
            "主線：%s" % payload.get("input", {}).get("title", ""),
            "主題：%s｜方向：%s｜強度：%s" % (
                payload.get("topic", ""), payload.get("direction", ""), payload.get("strength_word", "")),
            "結論：%s" % payload.get("verdict", ""),
        ]
        for row in payload.get("assets", []):
            lines.append("· %s %s（%s）" % (row["symbol"], row["direction"], row["horizon"]))
        conf = payload.get("confidence") or {}
        lines.append("置信度 %.2f（%s）" % (conf.get("score", 0), conf.get("level", "")))
        lines += ["", "⚠️ %s" % payload.get("guard", {}).get("note", "")]
        return "\n".join(lines)[:MAX_LEN]

    conf = payload.get("confidence") or {}
    lines = [
        "📊 PrismQuant 晨報 · %s" % (payload.get("generated_at", "")[5:16].replace("T", " ")),
        "",
        "【主線】%s" % payload.get("headline", ""),
        "",
        "【發生了什麼】",
    ]
    for i, item in enumerate((payload.get("sections") or {}).get("what_happened", []), 1):
        lines.append("%d. %s" % (i, item))
    lines.append("")
    lines.append("【這意味著什麼】")
    for i, item in enumerate((payload.get("sections") or {}).get("so_what", []), 1):
        lines.append("%d. %s" % (i, item))
    watch = (payload.get("sections") or {}).get("watch", [])
    if watch:
        lines.append("")
        lines.append("【要盯什麼】")
        for w in watch[:3]:
            lines.append("· %s｜觸發：%s" % (w.get("item", ""), w.get("trigger", "")))
    if payload.get("conflicts"):
        lines.append("")
        lines.append("【分歧】")
        for c in payload["conflicts"][:2]:
            lines.append("· %s（A1：%s｜A2：%s）" % (c["topic"], _short(c["a1"]), _short(c["a2"])))
    lines += [
        "",
        "置信度 %.2f（%s）｜%s 條證據｜%s 個獨立媒體" % (
            conf.get("score", 0), conf.get("level", ""), len(payload.get("evidence", [])),
            (payload.get("data_quality") or {}).get("distinct_publishers", 0)),
        "⚠️ %s" % payload.get("guard", {}).get("note", ""),
        "🔗 %s/#brief/%s" % (settings.web_base_url.rstrip("/"), payload.get("run_id", "")),
    ]
    text = "\n".join(lines)
    return text[:MAX_LEN]


def _short(text: str, n: int = 28) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[:n] + "…"


def send(text: str) -> dict:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return {
            "ok": True, "dry_run": True, "chars": len(text), "text": text,
            "note": "未設定 Telegram token / chat_id，這是預覽（不會真的發出）。",
        }
    url = "https://api.telegram.org/bot%s/sendMessage" % settings.telegram_bot_token
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": True,
            })
        ok = resp.status_code < 400
        return {"ok": ok, "dry_run": False, "chars": len(text), "text": text,
                "status": resp.status_code, "error": None if ok else resp.text[:200]}
    except Exception as exc:
        return {"ok": False, "dry_run": False, "chars": len(text), "text": text,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:150])}


def handle_command(text: str) -> str:
    text = (text or "").strip()
    cmd, _, rest = text.partition(" ")
    cmd = cmd.lower()

    if cmd in {"/start", "/help"}:
        return ("PrismQuant A5 指揮官\n"
                "/brief [問題] — 今日晨報\n"
                "/status — 上游與大腦狀態\n"
                "/probe <新聞> — 單篇探針\n"
                "注意：A5 只有建議權，A4 風控上線前不產生下單指令。")

    if cmd == "/status":
        h = connectors.health()
        b = brains.plan().as_dict()
        s = ledger.stats()
        return ("A1 %s｜A2 %s｜A4 %s\n大腦：%s（%s）\n提示詞：%s\n累計 %d 次 run，平均 %d ms" % (
            h["a1"]["status"], h["a2"]["status"], h["a4"]["status"],
            b["label"], b["topology"], b["prompt_variant"], s["total"], s["avg_latency_ms"]))

    if cmd == "/probe":
        if not rest.strip():
            return "用法：/probe 貼一段新聞內容"
        payload = commander.probe("", rest, "telegram")
        return build_message(payload)

    if cmd == "/brief":
        payload = commander.run(rest.strip() or "生成今天的晨報")
        return build_message(payload)

    return "未知指令。輸入 /help 看可用指令。"
