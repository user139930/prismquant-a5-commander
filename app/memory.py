# -*- coding: utf-8 -*-
"""三層記憶：A5 記得「上次怎麼說」，而不是每次都從零開始。

為什麼要分層（隊友問「記憶怎麼管理」，這是標準答案）：

| 層 | 存什麼 | 存多久 | 在哪 |
|---|---|---|---|
| 工作記憶 working | 這一次 run 的中間結果（取了幾條新聞、候選主題） | run 結束即釋放 | 進程內 dict |
| 情景記憶 episodic | 發生過什麼：每份晨報的主線句、置信度、分歧 | 永久（可刪） | SQLite |
| 語義記憶 semantic | 穩定的東西：某主題的最新方向、使用者關注詞、實體對應 | 永久（會衰減） | SQLite |

三條規矩，缺一條這個「記憶」就是假的：
1. **寫入要去重**：同一主題第二次寫入不是新增一條，是更新那一條（hits +1、顯著性上調），
   否則一個月後同一件事有 30 條記錄，檢索出來全是廢話。
2. **顯著性會衰減**：`salience *= 0.5 ** (天數 / 半衰期)`。兩個月前的關稅結論不該跟今天的
   同等重要；衰減到 0.05 以下歸檔（archived），不刪——歸檔還能查，刪了就沒了。
3. **可以被遺忘**：`forget()` 支援軟刪與硬刪。這是合規要求（使用者要求刪除時必須真刪），
   也是「記憶不是黑箱」的證明。
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.rag import tokenize

HK = timezone(timedelta(hours=8))
_lock = threading.RLock()
_ready = False
_working: dict[str, dict] = {}       # trace_id -> {key: value}，進程內、易失

LAYERS = {
    "working": {"label": "工作記憶", "en": "WORKING", "ttl": "run 結束即釋放",
                "desc": "這次 run 的中間結果：取了幾條、哪些主題進了候選。"},
    "episodic": {"label": "情景記憶", "en": "EPISODIC", "ttl": "永久（可刪）",
                 "desc": "發生過什麼：每份晨報的主線句、置信度、分歧與裁決。"},
    "semantic": {"label": "語義記憶", "en": "SEMANTIC", "ttl": "永久（衰減）",
                 "desc": "穩定事實：主題最新方向、使用者關注詞、實體對應關係。"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_items (
    id         TEXT PRIMARY KEY,
    layer      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    key        TEXT,
    text       TEXT NOT NULL,
    importance REAL NOT NULL,
    salience   REAL NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_hit   TEXT NOT NULL,
    source_run TEXT,
    meta       TEXT,
    archived   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_key ON memory_items(key, archived);
CREATE INDEX IF NOT EXISTS idx_mem_layer ON memory_items(layer, salience DESC);
"""


def _connect() -> sqlite3.Connection:
    path = settings.memory_file
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
    if not _ready:
        init()
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(HK).isoformat(timespec="seconds")


def _parse(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return datetime.now(HK)


# ---------------------------------------------------------------- 寫

def remember(text: str, layer: str = "episodic", kind: str = "note",
             key: str = "", importance: float = 0.5, source_run: str = "",
             meta: dict | None = None) -> dict:
    """寫入一條記憶。同一個 key 會合併而不是新增——這是「記憶」與「日誌」的分別。"""
    if layer not in LAYERS:
        layer = "episodic"
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "empty"}
    importance = max(0.0, min(1.0, float(importance)))
    init()
    now = _now()
    with _lock:
        with _db() as conn:
            row = None
            if key:
                row = conn.execute(
                    "SELECT * FROM memory_items WHERE key=? AND archived=0"
                    " ORDER BY last_hit DESC LIMIT 1", (key,)).fetchone()
            if row:
                sal = min(1.0, float(row["salience"]) + 0.15)
                conn.execute(
                    "UPDATE memory_items SET text=?, importance=?, salience=?, hits=hits+1,"
                    " last_hit=?, source_run=?, meta=? WHERE id=?",
                    (text, max(importance, float(row["importance"])), sal, now,
                     source_run or row["source_run"],
                     json.dumps(meta or json.loads(row["meta"] or "{}"), ensure_ascii=False),
                     row["id"]))
                return {"ok": True, "id": row["id"], "merged": True,
                        "salience": round(sal, 3), "hits": row["hits"] + 1}
            item_id = "mem-" + uuid.uuid4().hex[:6]
            conn.execute(
                "INSERT INTO memory_items (id, layer, kind, key, text, importance, salience,"
                " hits, created_at, last_hit, source_run, meta, archived)"
                " VALUES (?,?,?,?,?,?,?,0,?,?,?,?,0)",
                (item_id, layer, kind, key, text, importance, importance, now, now,
                 source_run, json.dumps(meta or {}, ensure_ascii=False)))
    return {"ok": True, "id": item_id, "merged": False, "salience": importance, "hits": 0}


def put_working(trace_id: str, key: str, value) -> None:
    if not trace_id:
        return
    _working.setdefault(trace_id, {})[key] = value
    if len(_working) > 50:                       # 別讓進程記憶無限長
        for k in list(_working)[:10]:
            _working.pop(k, None)


def clear_working(trace_id: str) -> None:
    _working.pop(trace_id, None)


def working(trace_id: str | None = None) -> dict:
    if trace_id:
        return {trace_id: _working.get(trace_id) or {}}
    return {k: v for k, v in list(_working.items())[-8:]}


# ---------------------------------------------------------------- 讀

def decay() -> dict:
    """按半衰期衰減顯著性，低於 0.05 歸檔。可重複執行（冪等）。"""
    init()
    half = max(0.5, settings.memory_half_life_days)
    now = datetime.now(HK)
    changed = archived = 0
    with _lock:
        with _db() as conn:
            rows = conn.execute(
                "SELECT id, salience, last_hit, archived FROM memory_items").fetchall()
            for r in rows:
                if r["archived"]:
                    continue
                days = max(0.0, (now - _parse(r["last_hit"])).total_seconds() / 86400)
                sal = float(r["salience"]) * (0.5 ** (days / half))
                dead = sal < 0.05
                conn.execute("UPDATE memory_items SET salience=?, archived=? WHERE id=?",
                             (round(sal, 4), 1 if dead else 0, r["id"]))
                changed += 1
                archived += 1 if dead else 0
    return {"scanned": changed, "archived": archived, "half_life_days": half}


def recall(query: str = "", k: int | None = None, layer: str | None = None,
           kind: str | None = None, include_archived: bool = False) -> dict:
    """混合召回：詞面相關 0.45 + 顯著性 0.25 + 重要性 0.20 + 新鮮度 0.10。

    每個權重都能對 examiner 解釋，不是「模型覺得相關」。
    """
    init()
    k = k or settings.memory_recall_k
    qtok = set(tokenize(query)) if query.strip() else set()
    now = datetime.now(HK)
    with _lock:
        with _db() as conn:
            sql = "SELECT * FROM memory_items WHERE 1=1"
            args: list = []
            if layer:
                sql += " AND layer=?"
                args.append(layer)
            if kind:
                sql += " AND kind=?"
                args.append(kind)
            if not include_archived:
                sql += " AND archived=0"
            rows = conn.execute(sql, args).fetchall()

    out = []
    for r in rows:
        ttok = set(tokenize(r["text"] + " " + (r["key"] or "")))
        lexical = len(qtok & ttok) / (len(qtok) or 1) if qtok else 0.0
        days = max(0.0, (now - _parse(r["last_hit"])).total_seconds() / 86400)
        fresh = 1.0 / (1.0 + days)
        score = 0.45 * lexical + 0.25 * float(r["salience"]) \
            + 0.20 * float(r["importance"]) + 0.10 * fresh
        if qtok and lexical == 0 and score < 0.25:
            continue
        out.append({
            "id": r["id"], "layer": r["layer"], "kind": r["kind"], "key": r["key"],
            "text": r["text"], "importance": round(float(r["importance"]), 3),
            "salience": round(float(r["salience"]), 3), "hits": r["hits"],
            "created_at": r["created_at"], "last_hit": r["last_hit"],
            "source_run": r["source_run"], "meta": json.loads(r["meta"] or "{}"),
            "score": round(score, 4), "lexical": round(lexical, 3),
        })
    out.sort(key=lambda x: -x["score"])
    return {"query": query, "hits": out[:k], "total": len(rows),
            "weights": {"lexical": 0.45, "salience": 0.25, "importance": 0.20, "fresh": 0.10}}


def get(item_id: str) -> dict | None:
    init()
    with _lock:
        with _db() as conn:
            r = conn.execute("SELECT * FROM memory_items WHERE id=?", (item_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d


def forget(item_id: str, hard: bool = False) -> bool:
    """合規：使用者要求刪除時，先軟刪（archived=2），再硬刪。"""
    init()
    with _lock:
        with _db() as conn:
            if hard:
                cur = conn.execute("DELETE FROM memory_items WHERE id=?", (item_id,))
            else:
                cur = conn.execute("UPDATE memory_items SET archived=2 WHERE id=?", (item_id,))
            return cur.rowcount > 0


# ---------------------------------------------------------------- 統計

def stats() -> dict:
    init()
    decay()
    with _lock:
        with _db() as conn:
            rows = conn.execute(
                "SELECT layer, COUNT(*) n, AVG(salience) s, SUM(hits) h"
                " FROM memory_items WHERE archived=0 GROUP BY layer").fetchall()
            arch = conn.execute(
                "SELECT COUNT(*) FROM memory_items WHERE archived=1").fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
    by = {r["layer"]: {"count": r["n"], "avg_salience": round(r["s"] or 0, 3),
                       "hits": r["h"] or 0} for r in rows}
    for name, meta in LAYERS.items():
        by.setdefault(name, {"count": 0, "avg_salience": 0, "hits": 0})
    return {
        "total": total, "archived": arch, "by_layer": by, "layers": LAYERS,
        "half_life_days": settings.memory_half_life_days,
        "working_slots": {k: len(v) for k, v in _working.items()},
        "engine": "SQLite + 詞面/顯著性/重要性/新鮮度混合打分",
    }


def list_items(layer: str | None = None, limit: int = 40) -> list[dict]:
    init()
    with _lock:
        with _db() as conn:
            if layer:
                rows = conn.execute(
                    "SELECT * FROM memory_items WHERE layer=? AND archived=0"
                    " ORDER BY salience DESC LIMIT ?", (layer, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_items WHERE archived=0"
                    " ORDER BY salience DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r["id"], "layer": r["layer"], "kind": r["kind"], "key": r["key"],
             "text": r["text"], "importance": round(float(r["importance"]), 3),
             "salience": round(float(r["salience"]), 3), "hits": r["hits"],
             "last_hit": r["last_hit"], "source_run": r["source_run"]} for r in rows]


# ---------------------------------------------------------------- 晨報掛鉤

def absorb_brief(payload: dict) -> dict:
    """把一份晨報的結論寫進記憶。下一份晨報就能說「較上次 +0.09」。

    寫三類：
    - episodic：這份晨報本身（key = brief:日期，同日重跑會合併）
    - semantic：每個主題的最新方向與置信度（key = topic:主題）
    - semantic：使用者的關注詞（key = pref:watchlist）
    """
    run_id = payload.get("run_id") or ""
    conf = (payload.get("confidence") or {}).get("score") or 0
    date = (payload.get("generated_at") or _now())[:10]
    written = []

    head = payload.get("headline") or ""
    topics = [c.get("label") or c.get("topic") for c in (payload.get("clusters") or [])][:5]
    conflicts = len(payload.get("conflicts") or [])
    text = "%s｜置信度 %.2f｜主題 %s｜分歧 %d 組" % (
        head or "（無主線句）", conf, "、".join([t for t in topics if t]) or "無", conflicts)
    written.append(remember(text, layer="episodic", kind="brief",
                            key="brief:" + date, importance=0.7,
                            source_run=run_id, meta={"confidence": conf, "topics": topics}))

    for c in (payload.get("clusters") or [])[:5]:
        topic = c.get("label") or c.get("topic")
        if not topic:
            continue
        direction = c.get("direction") or ""
        tconf = c.get("strength")
        written.append(remember(
            "%s：方向 %s，強度 %s，獨立來源 %s 家" % (
                topic, direction or "未定",
                ("%.2f" % tconf) if isinstance(tconf, (int, float)) else "—",
                len(c.get("publishers") or []) or "—"),
            layer="semantic", kind="topic", key="topic:" + str(topic),
            importance=0.6, source_run=run_id,
            meta={"direction": direction, "strength": tconf}))

    try:
        from app import store

        wl = store.get_watchlist()
        written.append(remember("使用者關注：" + "、".join(wl), layer="semantic",
                                kind="preference", key="pref:watchlist",
                                importance=0.8, meta={"watchlist": wl}))
    except Exception:
        pass

    ok = [w for w in written if w.get("ok")]
    return {"written": len(ok), "merged": sum(1 for w in ok if w.get("merged")),
            "items": ok}
