# -*- coding: utf-8 -*-
"""工具註冊表：把「代理能做的事」從散落的函數呼叫變成可列舉、可授權、可審計的工具。

隊友問「工具用到了什麼」，這個問題的正確答案不是列一堆庫名（爬蟲、RSS、httpx），
而是：**A5 自己能執行的動作有哪幾個、每個的參數契約是什麼、權限級別是什麼、誰在什麼
時候調過它**。所以這裡做四件事：

1. **註冊**：每個工具都有 name / description / JSON Schema 參數 / 權限 / 超時 / 是否冪等。
2. **導出**：`specs()` 直接輸出 OpenAI function calling 格式，可以整包餵給大模型讓它選工具，
   不用在提示詞裡用自然語言描述「你可以查新聞」。
3. **授權**：`read` / `compute` / `write` 三級。專案定位是研究輔助，**不存在下單工具**；
   名稱含 order / trade / place 的一律拒絕註冊，這是硬規則不是註解。
4. **審計**：每次呼叫寫 `tool_calls` 表（含 trace_id、參數、耗時、錯誤），
   事後能回答「這條結論是哪個工具算的」。

A5 的工具邊界（重要，別跟隊友的工具混為一談）：
- 屬 A5：匯總、交叉驗證、記憶、知識庫檢索、置信度、模擬風控閘門、晨報比較。
- 屬 A1：抓新聞、RSS/爬蟲、去重、翻譯。屬 A2：政要抓取、實體識別。
  A5 不重複造，只透過 bus 呼叫（`news.query` / `political.query`）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.config import settings

HK = timezone(timedelta(hours=8))
_lock = threading.RLock()
_ready = False
_registry: dict[str, "Tool"] = {}

FORBIDDEN = ("order", "place", "trade", "execute", "withdraw", "transfer")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id         TEXT PRIMARY KEY,
    trace_id   TEXT,
    name       TEXT NOT NULL,
    args       TEXT,
    ok         INTEGER,
    ms         INTEGER,
    error      TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_trace ON tool_calls(trace_id, created_at);
"""


@dataclass
class Tool:
    name: str
    title: str
    description: str
    params: dict
    handler: Callable[..., Any]
    permission: str = "read"          # read | compute | write
    idempotent: bool = True
    timeout_s: float = 8.0
    tags: list[str] = field(default_factory=list)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params,
            },
        }

    def brief(self) -> dict:
        props = (self.params.get("properties") or {})
        return {
            "name": self.name, "title": self.title, "description": self.description,
            "permission": self.permission, "idempotent": self.idempotent,
            "timeout_s": self.timeout_s, "tags": self.tags,
            "params": [{"name": k, "type": v.get("type", "string"),
                        "required": k in (self.params.get("required") or []),
                        "description": v.get("description", "")} for k, v in props.items()],
        }


def _connect() -> sqlite3.Connection:
    path = settings.tool_file
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


def tool(name: str, title: str, description: str, params: dict | None = None,
         permission: str = "read", idempotent: bool = True, timeout_s: float = 8.0,
         tags: list[str] | None = None):
    """裝飾器：把一個函數註冊成工具。"""
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        register(Tool(name=name, title=title, description=description,
                      params=params or {"type": "object", "properties": {}},
                      handler=fn, permission=permission, idempotent=idempotent,
                      timeout_s=timeout_s, tags=tags or []))
        return fn
    return wrap


def register(t: Tool) -> None:
    low = t.name.lower()
    if any(bad in low for bad in FORBIDDEN):
        raise ValueError("禁止註冊交易類工具：%s（本專案不下單）" % t.name)
    _registry[t.name] = t


def get(name: str) -> Tool | None:
    return _registry.get(name)


def specs(openai: bool = True) -> list[dict]:
    items = [_registry[k] for k in sorted(_registry)]
    return [t.schema() for t in items] if openai else [t.brief() for t in items]


def catalog() -> dict:
    items = [t.brief() for t in (_registry[k] for k in sorted(_registry))]
    by_perm: dict[str, int] = {}
    for t in items:
        by_perm[t["permission"]] = by_perm.get(t["permission"], 0) + 1
    return {
        "count": len(items), "tools": items, "by_permission": by_perm,
        "openai_ready": True,
        "policy": "不存在下單工具：名稱含 %s 一律拒絕註冊" % "/".join(FORBIDDEN),
        "boundary": "抓取類工具屬 A1/A2，A5 只透過 bus 呼叫，不重複造",
    }


def call(name: str, args: dict | None = None, trace_id: str = "") -> dict:
    """執行一個工具並寫審計。錯誤不拋，回 ok=False——上層據此降級。"""
    init()
    t = _registry.get(name)
    started = time.time()
    if not t:
        return {"ok": False, "tool": name, "error": "unknown_tool",
                "available": sorted(_registry)}
    try:
        data = t.handler(**(args or {}))
        ok, err = True, ""
    except TypeError as exc:
        data, ok, err = None, False, "bad_args: %s" % exc
    except Exception as exc:
        data, ok, err = None, False, "%s: %s" % (type(exc).__name__, str(exc)[:200])
    ms = int((time.time() - started) * 1000)
    with _db() as conn:
        conn.execute(
            "INSERT INTO tool_calls (id, trace_id, name, args, ok, ms, error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("tc-" + uuid.uuid4().hex[:8], trace_id, name,
             json.dumps(args or {}, ensure_ascii=False, default=str),
             1 if ok else 0, ms, err,
             datetime.now(HK).isoformat(timespec="seconds")))
    return {"ok": ok, "tool": name, "ms": ms, "data": data, "error": err,
            "permission": t.permission, "trace_id": trace_id}


def audit(limit: int = 30) -> list[dict]:
    init()
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM tool_calls ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,)).fetchall()
    return [{"id": r["id"], "trace_id": r["trace_id"], "name": r["name"],
             "args": json.loads(r["args"] or "{}"), "ok": bool(r["ok"]),
             "ms": r["ms"], "error": r["error"] or "", "created_at": r["created_at"]}
            for r in rows]


def audit_stats() -> dict:
    init()
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE ok=0").fetchone()[0]
        top = [{"name": r[0], "calls": r[1]} for r in conn.execute(
            "SELECT name, COUNT(*) c FROM tool_calls GROUP BY name ORDER BY c DESC LIMIT 8")]
        avg = conn.execute("SELECT AVG(ms) FROM tool_calls").fetchone()[0]
    return {"calls": total, "failed": failed, "avg_ms": round(avg or 0), "top": top}


# ---------------------------------------------------------------- 內建工具

@tool("status.get", "上游狀態", "讀 A1 / A2 / A4 的連線狀態與延遲，不做任何推論。",
      {"type": "object", "properties": {}}, permission="read", tags=["ops"])
def _status_get():
    from app import connectors

    h = connectors.health()
    return {k: {"ok": v.get("ok"), "latency_ms": v.get("latency_ms"),
                "status": v.get("status")} for k, v in h.items()}


@tool("news.query", "查新聞", "向 A1 取最近的新聞事件（代為轉發，不自己爬）。",
      {"type": "object", "properties": {
          "limit": {"type": "integer", "description": "取幾條，預設 40，上限 100"}}},
      permission="read", tags=["a1"])
def _news_query(limit: int = 40):
    from app import connectors

    events, meta = connectors.a1_events(max(1, min(100, int(limit))))
    return {"online": meta.get("ok"), "count": len(events),
            "events": [{"title": e.get("title"), "source": e.get("source"),
                        "published": e.get("published")} for e in events[:10]]}


@tool("political.query", "查政要表態", "向 A2 取最近的公開發言記錄（代為轉發）。",
      {"type": "object", "properties": {
          "limit": {"type": "integer", "description": "取幾條，預設 20"}}},
      permission="read", tags=["a2"])
def _political_query(limit: int = 20):
    from app import connectors

    events, meta = connectors.a2_events(max(1, min(100, int(limit))))
    return {"online": meta.get("ok"), "degraded": meta.get("degraded"),
            "count": len(events),
            "events": [{"title": e.get("title"), "direction": e.get("direction")}
                       for e in events[:10]]}


@tool("kb.search", "知識庫檢索", "在向量知識庫裡找來源目錄、術語定義、歷史晨報片段。",
      {"type": "object", "properties": {
          "query": {"type": "string", "description": "自然語言問題或關鍵詞"},
          "k": {"type": "integer", "description": "返回幾塊，預設 5"},
          "kind": {"type": "string", "description": "source / glossary / brief / note"}},
       "required": ["query"]}, permission="read", tags=["rag"])
def _kb_search(query: str, k: int = 5, kind: str | None = None):
    from app import rag

    r = rag.search(query, k=int(k), kind=kind or None)
    return {"backend": r["backend"], "hits": [
        {"ref": h["ref"], "score": h["score"], "title": h["title"],
         "text": h["text"][:180], "url": h["url"]} for h in r["hits"]]}


@tool("kb.ingest", "知識庫入庫", "把一份文本切塊向量化後寫進知識庫。",
      {"type": "object", "properties": {
          "title": {"type": "string"}, "text": {"type": "string"},
          "kind": {"type": "string", "description": "source / glossary / brief / note"}},
       "required": ["title", "text"]},
      permission="write", idempotent=False, tags=["rag"])
def _kb_ingest(title: str, text: str, kind: str = "note", source: str = "", url: str = ""):
    from app import rag

    return rag.ingest_document(title=title, text=text, kind=kind, source=source, url=url)


@tool("memory.recall", "記憶召回", "按問題檢索三層記憶，回傳打分與命中原因。",
      {"type": "object", "properties": {
          "query": {"type": "string"},
          "k": {"type": "integer", "description": "預設 5"},
          "layer": {"type": "string", "description": "working / episodic / semantic"}}},
      permission="read", tags=["memory"])
def _memory_recall(query: str = "", k: int = 5, layer: str | None = None):
    from app import memory

    r = memory.recall(query, k=int(k), layer=layer or None)
    return {"weights": r["weights"], "hits": [
        {"id": h["id"], "layer": h["layer"], "text": h["text"], "score": h["score"],
         "salience": h["salience"]} for h in r["hits"]]}


@tool("memory.write", "寫入記憶", "把一條結論寫進情景或語義記憶（同 key 會合併）。",
      {"type": "object", "properties": {
          "text": {"type": "string"},
          "layer": {"type": "string", "description": "episodic / semantic"},
          "kind": {"type": "string"}, "key": {"type": "string"},
          "importance": {"type": "number", "description": "0–1，預設 0.6"}},
       "required": ["text"]},
      permission="write", idempotent=False, tags=["memory"])
def _memory_write(text: str, layer: str = "episodic", kind: str = "note",
                  key: str = "", importance: float = 0.6):
    from app import memory

    return memory.remember(text, layer=layer, kind=kind, key=key, importance=importance)


@tool("confidence.compute", "算置信度", "用加減分規則算置信度：媒體數、單源、衝突、時效、樣本數據。",
      {"type": "object", "properties": {
          "sources": {"type": "integer", "description": "獨立來源數"},
          "conflicts": {"type": "integer", "description": "分歧組數"},
          "single_source_topics": {"type": "integer"},
          "sample_mode": {"type": "boolean"},
          "age_hours": {"type": "number"}},
       "required": ["sources"]}, permission="compute", tags=["analyze"])
def _confidence_compute(sources: int = 1, conflicts: int = 0,
                        single_source_topics: int = 0, sample_mode: bool = False,
                        age_hours: float = 0.0):
    score = 0.45
    score += min(0.30, max(0, int(sources) - 1) * 0.07)
    score -= min(0.20, max(0, int(conflicts)) * 0.05)
    score -= min(0.15, max(0, int(single_source_topics)) * 0.04)
    if age_hours > 24:
        score -= min(0.10, (age_hours - 24) / 240)
    if sample_mode:
        score = min(score, 0.55)
    score = max(0.05, min(0.95, score))
    return {"score": round(score, 2),
            "level": "高" if score >= 0.7 else ("中" if score >= 0.5 else "低"),
            "note": "規則加減分，非模型自評；輸入為樣本時上限 0.62"}


@tool("risk.gate", "風控閘門（模擬）", "對一份晨報的傳導鏈跑五條規則，回通過／降級／否決。",
      {"type": "object", "properties": {
          "run_id": {"type": "string", "description": "晨報 run_id"}},
       "required": ["run_id"]}, permission="compute", tags=["a4"])
def _risk_gate(run_id: str):
    from app import commander, ledger

    full = ledger.get(run_id)
    if not full:
        raise ValueError("找不到這份晨報：%s" % run_id)
    return commander.simulate_gate(full.get("payload") or {})


@tool("brief.diff", "晨報差異", "把某份晨報與上一份比較：新增／掉出的主題、方向翻轉、置信度變化。",
      {"type": "object", "properties": {"run_id": {"type": "string"}},
       "required": ["run_id"]}, permission="read", tags=["analyze"])
def _brief_diff(run_id: str):
    from app import commander

    return commander.diff_with_previous(run_id)
