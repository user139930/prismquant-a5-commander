# -*- coding: utf-8 -*-
"""定時晨報：每天固定時間自動生成並推送。

為什麼要有：A5 的產品形態是「早上打開就有一份」，不是「按一下才有一份」。
沒有排程，它只是一個 demo 頁面；有了排程，它才是一個會自己幹活的 agent。

實作取捨：
- 用 asyncio 的背景任務，不引入 Celery/APScheduler（多一個依賴、多一個要跑的進程）。
- 每 30 秒醒一次看時間，而不是 sleep 到明天——這樣改 .env 之後不用重啟也能生效。
- 生成走 `commander.run()`，跟手動按鈕完全同一條路徑，所以護欄、留檔、降級都一樣。
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from app import commander, telegram
from app.config import settings

HK = dt.timezone(dt.timedelta(hours=8))

STATE: dict[str, Any] = {
    "last_run": None,       # ISO 時間
    "last_run_id": None,
    "last_ok": None,
    "last_error": None,
    "last_delivery": None,
    "next_run": None,
    "fired": 0,
}

_task: asyncio.Task | None = None


def _parse_time(value: str) -> tuple[int, int]:
    try:
        hh, mm = (value or "08:30").split(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:
        return 8, 30


def next_run_at(now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now(HK)
    hh, mm = _parse_time(settings.schedule_time)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target


def status() -> dict:
    hh, mm = _parse_time(settings.schedule_time)
    return {
        "enabled": settings.schedule_enabled,
        "time": "%02d:%02d" % (hh, mm),
        "question": settings.schedule_question,
        "next_run": STATE["next_run"],
        "last_run": STATE["last_run"],
        "last_run_id": STATE["last_run_id"],
        "last_ok": STATE["last_ok"],
        "last_error": STATE["last_error"],
        "last_delivery": STATE["last_delivery"],
        "fired": STATE["fired"],
        "running": _task is not None and not _task.done(),
    }


async def fire(reason: str = "scheduled") -> dict:
    """跑一次並推送。手動「立即執行」也走這裡。"""
    started = dt.datetime.now(HK)
    try:
        payload = await asyncio.to_thread(commander.run, settings.schedule_question)
        text = telegram.build_message(payload) if payload.get("kind") == "brief" \
            else "%s\n\n%s" % (payload.get("reply", ""), payload.get("context", ""))
        delivery = telegram.send(text)
        STATE.update({
            "last_run": started.isoformat(timespec="seconds"),
            "last_run_id": payload.get("run_id"),
            "last_ok": bool(payload.get("kind") == "brief"),
            "last_error": None if payload.get("kind") == "brief" else "非晨報輸出（可能是對話）",
            "last_delivery": "sent" if delivery.get("ok") and not delivery.get("dry_run")
                              else ("preview" if delivery.get("dry_run") else "failed"),
        })
        STATE["fired"] += 1
        return {"ok": True, "run_id": payload.get("run_id"),
                "kind": payload.get("kind"), "delivery": delivery.get("dry_run") and "preview" or "sent",
                "reason": reason}
    except Exception as exc:
        STATE.update({"last_run": started.isoformat(timespec="seconds"),
                      "last_ok": False, "last_error": str(exc)[:200]})
        return {"ok": False, "error": str(exc)[:300]}


async def _loop() -> None:
    while True:
        try:
            if not settings.schedule_enabled:
                STATE["next_run"] = None
                await asyncio.sleep(30)
                continue

            nxt = next_run_at()
            STATE["next_run"] = nxt.isoformat(timespec="seconds")
            delay = (nxt - dt.datetime.now(HK)).total_seconds()

            if delay > 90:
                # 分段睡，改設定不用重啟
                await asyncio.sleep(min(delay - 30, 300))
                continue

            if delay > 0:
                await asyncio.sleep(delay)

            await fire("scheduled")
            # 跑完之後把 next_run 推到下一天，避免同一分鐘重複觸發
            await asyncio.sleep(61)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 背景任務不能死
            STATE["last_error"] = str(exc)[:200]
            await asyncio.sleep(60)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="a5-scheduler")


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None
