# -*- coding: utf-8 -*-
"""Run Ledger：每一次 A5 的輸出都留檔，可回放、可對照。

proposal §3.4 要求「所有消息持久化，支持 replay this decision」。這裡做最小可用版：
每一次 run 存一份完整 payload + 當時的輸入快照 + 拓撲 / 提示詞版本。
Replay 不是重播舊畫面，而是「用同一份輸入重跑一次」，然後比對置信度與分歧有沒有漂移。

用標準庫 sqlite3，零額外依賴：examiner 在自己的電腦上 clone 下來也能直接跑。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.config import settings

HK = timezone(timedelta(hours=8))
_lock = threading.RLock()
_ready = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    question      TEXT,
    mode          TEXT,
    topology      TEXT,
    prompt_variant TEXT,
    latency_ms    INTEGER,
    confidence    REAL,
    guard_passed  INTEGER,
    payload       TEXT NOT NULL,
    snapshot      TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
"""


def _connect() -> sqlite3.Connection:
    path = settings.ledger_file
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    global _ready
    with _lock:
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
    _ready = True


@contextmanager
def _db():
    """自建表 + 保證關閉連線（sqlite3 的 with 只做事務，不關連線）。"""
    if not _ready:
        init()
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def new_id(prefix: str = "r") -> str:
    return "%s-%s-%s" % (prefix, datetime.now(HK).strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:4])


def save(payload: dict, snapshot: dict | None = None) -> str:
    run_id = payload.get("run_id") or new_id("r" if payload.get("kind") == "brief" else "p")
    payload["run_id"] = run_id
    conf = payload.get("confidence") or {}
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, created_at, kind, question, mode, topology,"
            " prompt_variant, latency_ms, confidence, guard_passed, payload, snapshot)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                datetime.now(HK).isoformat(timespec="seconds"),
                payload.get("kind", "brief"),
                payload.get("question") or (payload.get("input") or {}).get("title", ""),
                (payload.get("brain") or {}).get("mode", ""),
                (payload.get("brain") or {}).get("topology", ""),
                (payload.get("brain") or {}).get("prompt_variant", ""),
                (payload.get("stats") or {}).get("latency_ms"),
                float(conf.get("score") or 0),
                1 if (payload.get("guard") or {}).get("passed") else 0,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(snapshot or {}, ensure_ascii=False),
            ),
        )
    return run_id


def list_runs(limit: int = 30) -> list[dict]:
    with _db() as conn:
        try:
            rows = conn.execute(
                "SELECT run_id, created_at, kind, question, mode, topology, prompt_variant,"
                " latency_ms, confidence, guard_passed,"
                " json_extract(payload, '$.headline') AS headline"
                " FROM runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT run_id, created_at, kind, question, mode, topology, prompt_variant,"
                " latency_ms, confidence, guard_passed, NULL AS headline"
                " FROM runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
    return [dict(r) for r in rows]


def get(run_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    d["snapshot"] = json.loads(d["snapshot"] or "{}")
    return d


def stats() -> dict:
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, AVG(latency_ms) lat, SUM(1 - guard_passed) blocked FROM runs"
        ).fetchone()
        today = datetime.now(HK).date().isoformat()
        day = conn.execute(
            "SELECT COUNT(*) n FROM runs WHERE substr(created_at,1,10)=?", (today,)
        ).fetchone()
    return {
        "total": row["n"] or 0,
        "avg_latency_ms": int(row["lat"] or 0),
        "guard_blocked": row["blocked"] or 0,
        "today": day["n"] or 0,
    }
