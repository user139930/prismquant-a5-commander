# -*- coding: utf-8 -*-
"""Agent 訊息總線：A5 與遠端代理（A1 / A2 / A3 / A4）之間唯一允許的說話方式。

隊友問「遠端 agent 是怎麼通信的」，答案是三句話：

1. **不共享內存，只發信封。** 每個代理是一個進程，跑在自己的端口（A1:8010、A2:8120、
   A5:8130），彼此只能透過 HTTP 交換 JSON 信封。信封欄位固定：誰發的、發給誰、
   什麼方法、參數、trace_id、證據、時間。
2. **有身份證才叫可追溯。** 一次使用者請求生成一個 `trace_id`，之後每一次呼叫
   （含失敗、重試、降級）都帶同一個 trace_id 落庫。驗收時能把整條決策鏈按時間印出來。
3. **失敗要留下證據，不能假裝成功。** 超時 → 標 `failed` + 錯誤碼，上層據此降級；
   只有冪等的方法才自動重試。靜默失敗（看起來成功、其實沒拿到資料）是禁止的。

同時實作 A2A 的兩個標準動作：
- `GET /.well-known/agent-card.json`（能力發現：我是誰、會什麼、端點在哪）
- `POST /api/v1/a2a/inbox`（收件匣：隊友可以主動推訊息或發 JSON-RPC 請求）

儲存用 SQLite，因為訊息日誌要能在重啟後還在——內存隊列一重啟就沒了，等於沒有審計。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.config import settings

HK = timezone(timedelta(hours=8))
_lock = threading.RLock()
_ready = False
_handlers: dict[str, Callable[[dict], Any]] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_messages (
    msg_id         TEXT PRIMARY KEY,
    trace_id       TEXT NOT NULL,
    correlation_id TEXT,
    sender         TEXT NOT NULL,
    recipient      TEXT NOT NULL,
    method         TEXT NOT NULL,
    type           TEXT NOT NULL,
    status         TEXT NOT NULL,
    payload        TEXT,
    evidence       TEXT,
    error          TEXT,
    latency_ms     INTEGER,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_trace ON agent_messages(trace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_msg_to ON agent_messages(recipient, created_at DESC);
"""

# 這些方法可以安全重試（沒有副作用）；其餘一律不重試
IDEMPOTENT = {"agent.ping", "status.get", "news.query", "political.query",
              "memory.recall", "kb.search"}


class AgentMessage(BaseModel):
    """A2A 信封。欄位刻意少而穩定：多一個欄位就多一個版本相容問題。"""
    msg_id: str = Field(default_factory=lambda: "m-" + uuid.uuid4().hex[:10])
    trace_id: str = ""
    correlation_id: str = ""
    sender: str = "A5"
    recipient: str = ""
    method: str = ""
    type: str = "request"          # request | response | event | error
    status: str = "pending"        # pending | ok | failed
    payload: dict = Field(default_factory=dict)
    evidence: list = Field(default_factory=list)
    error: str = ""
    latency_ms: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(HK).isoformat(timespec="seconds"))


def _connect() -> sqlite3.Connection:
    path = settings.bus_file
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


def new_trace_id(prefix: str = "t") -> str:
    return "%s-%s-%s" % (prefix, datetime.now(HK).strftime("%H%M%S"), uuid.uuid4().hex[:6])


def register(method: str, fn: Callable[[dict], Any]) -> None:
    """註冊 A5 自己能回應的方法。隊友呼叫時就是走這裡。"""
    _handlers[method] = fn


def methods() -> list[str]:
    return sorted(_handlers)


# ---------------------------------------------------------------- 寫入

def send(msg: AgentMessage) -> AgentMessage:
    if not msg.trace_id:
        msg.trace_id = new_trace_id()
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_messages (msg_id, trace_id, correlation_id, sender,"
            " recipient, method, type, status, payload, evidence, error, latency_ms, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (msg.msg_id, msg.trace_id, msg.correlation_id, msg.sender, msg.recipient,
             msg.method, msg.type, msg.status,
             json.dumps(msg.payload, ensure_ascii=False),
             json.dumps(msg.evidence, ensure_ascii=False),
             msg.error, msg.latency_ms, msg.created_at),
        )
    return msg


def publish(event: str, payload: dict, trace_id: str = "", sender: str = "A5") -> AgentMessage:
    """事件廣播：不是請求，不需要回覆。例如 brief.generated、memory.written。"""
    return send(AgentMessage(trace_id=trace_id, sender=sender, recipient="*",
                             method=event, type="event", status="ok", payload=payload))


# ---------------------------------------------------------------- 遠端呼叫

def _base_url(agent: str) -> str:
    return {
        "A1": settings.a1_api_url,
        "A2": settings.a2_api_url,
        "A4": settings.a4_api_url,
    }.get(agent.upper(), "")


def request(recipient: str, method: str, payload: dict | None = None,
            trace_id: str = "", timeout: float | None = None,
            sender: str = "A5", retries: int | None = None) -> AgentMessage:
    """發一個請求並等回覆。

    兩種情況：
    - 收件人在本地有 handler（A5 自己、或隊友在同一進程註冊的模擬器）→ 直接執行。
    - 否則走 HTTP：`POST {base}/api/v1/a2a/inbox`，body 是信封。
    任何失敗都回傳 status="failed" 的信封，不拋異常——呼叫方看 status 決定降級。
    """
    init()
    req = AgentMessage(trace_id=trace_id or new_trace_id(), sender=sender,
                       recipient=recipient, method=method, type="request",
                       payload=payload or {})
    started = time.time()

    # 1) 本地 handler
    if recipient.upper() in {"A5", "SELF"} and method in _handlers:
        try:
            result = _handlers[method](req.payload)
            req.status = "ok"
            req.payload = {"request": req.payload, "result": result}
        except Exception as exc:                     # 工具/記憶層的錯不該炸掉整條鏈
            req.status = "failed"
            req.error = "%s: %s" % (type(exc).__name__, exc)[:200]
        req.latency_ms = int((time.time() - started) * 1000)
        send(req)
        return req

    # 2) 遠端 HTTP
    base = _base_url(recipient)
    if not base:
        req.status = "failed"
        req.error = "no_endpoint: %s 未設定端點（A4 尚未接入）" % recipient
        req.latency_ms = int((time.time() - started) * 1000)
        send(req)
        return req

    tries = retries if retries is not None else (2 if method in IDEMPOTENT else 1)
    last_error = ""
    for attempt in range(tries):
        try:
            import httpx

            body = req.model_dump()
            body["correlation_id"] = req.msg_id
            r = httpx.post(base.rstrip("/") + "/api/v1/a2a/inbox", json=body,
                           timeout=timeout or settings.upstream_timeout)
            r.raise_for_status()
            data = r.json()
            req.status = "ok"
            req.payload = {"request": req.payload, "result": data.get("result", data)}
            req.latency_ms = int((time.time() - started) * 1000)
            send(req)
            return req
        except Exception as exc:
            last_error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            if attempt < tries - 1:
                time.sleep(0.3 * (2 ** attempt))
    req.status = "failed"
    req.error = last_error
    req.latency_ms = int((time.time() - started) * 1000)
    send(req)
    return req


def handle_incoming(body: dict) -> dict:
    """收件匣：接受 A2A 信封，也接受 JSON-RPC 2.0（隊友用 curl 比較順手）。

    回傳永遠是「同一個 trace_id 的 response 信封」。
    """
    init()
    if "jsonrpc" in body:
        method = body.get("method") or ""
        params = body.get("params") or {}
        trace_id = body.get("trace_id") or new_trace_id()
        msg = AgentMessage(trace_id=trace_id, sender=body.get("from") or "external",
                           recipient="A5", method=method, type="request",
                           payload=params if isinstance(params, dict) else {"value": params})
        rpc_id = body.get("id")
    else:
        msg = AgentMessage(**{k: v for k, v in body.items()
                              if k in AgentMessage.model_fields})
        rpc_id = None
        if not msg.trace_id:
            msg.trace_id = new_trace_id()

    started = time.time()
    fn = _handlers.get(msg.method)
    if not fn:
        msg.status, msg.error = "failed", "unknown_method: %s（可用：%s）" % (
            msg.method, ", ".join(methods()))
        msg.type = "error"
    else:
        try:
            msg.payload = {"request": msg.payload, "result": fn(msg.payload)}
            msg.status = "ok"
        except Exception as exc:
            msg.status, msg.error = "failed", "%s: %s" % (type(exc).__name__, exc)[:200]
            msg.type = "error"
    msg.recipient, msg.sender = msg.sender, "A5"
    msg.type = "response" if msg.type != "error" else "error"
    msg.latency_ms = int((time.time() - started) * 1000)
    send(msg)
    out = msg.model_dump()
    out["received_from"] = body.get("sender") or body.get("from") or "external"
    if rpc_id is not None:
        if msg.status == "ok":
            return {"jsonrpc": "2.0", "id": rpc_id, "result": out["payload"].get("result")}
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32601, "message": msg.error}}
    return out


# ---------------------------------------------------------------- 讀取

def _row(r: sqlite3.Row) -> dict:
    return {
        "msg_id": r["msg_id"], "trace_id": r["trace_id"],
        "correlation_id": r["correlation_id"], "sender": r["sender"],
        "recipient": r["recipient"], "method": r["method"], "type": r["type"],
        "status": r["status"], "payload": json.loads(r["payload"] or "{}"),
        "evidence": json.loads(r["evidence"] or "[]"), "error": r["error"] or "",
        "latency_ms": r["latency_ms"], "created_at": r["created_at"],
    }


def messages(limit: int = 30, agent: str | None = None) -> list[dict]:
    init()
    with _db() as conn:
        if agent:
            rows = conn.execute(
                "SELECT * FROM agent_messages WHERE recipient=? OR sender=?"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?", (agent, agent, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_messages ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
    return [_row(r) for r in rows]


def trace(trace_id: str) -> dict:
    init()
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_messages WHERE trace_id=? ORDER BY created_at, rowid",
            (trace_id,)).fetchall()
    msgs = [_row(r) for r in rows]
    return {"trace_id": trace_id, "count": len(msgs), "messages": msgs,
            "failed": sum(1 for m in msgs if m["status"] == "failed")}


def stats() -> dict:
    init()
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM agent_messages").fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM agent_messages WHERE status='failed'").fetchone()[0]
        by_agent = {r[0]: r[1] for r in conn.execute(
            "SELECT recipient, COUNT(*) FROM agent_messages GROUP BY recipient").fetchall()}
        traces = conn.execute(
            "SELECT COUNT(DISTINCT trace_id) FROM agent_messages").fetchone()[0]
        avg = conn.execute(
            "SELECT AVG(latency_ms) FROM agent_messages WHERE type!='event'").fetchone()[0]
    return {"messages": total, "traces": traces, "failed": failed,
            "by_recipient": by_agent, "avg_latency_ms": round(avg or 0),
            "methods": methods()}


def card() -> dict:
    """Agent Card：A2A 的能力發現端點。隊友的代理先讀這張卡，才知道能叫我做什麼。"""
    return {
        "name": "PrismQuant A5 Commander",
        "description": "匯總 A1 新聞與 A2 政要監控，做交叉驗證、分歧裁決、情境推演，輸出決策級晨報。",
        "version": "1.0.0",
        "protocol": "a2a/1.0 + json-rpc-2.0",
        "endpoints": {
            "inbox": "/api/v1/a2a/inbox",
            "card": "/.well-known/agent-card.json",
            "health": "/health",
        },
        "capabilities": [
            {"method": m, "description": _cap_desc(m)} for m in methods()
        ],
        "input_schema": {
            "type": "object",
            "required": ["method"],
            "properties": {
                "method": {"type": "string", "enum": methods()},
                "params": {"type": "object"},
                "trace_id": {"type": "string", "description": "同一條決策鏈共用一個 id"},
            },
        },
        "limits": {
            "advisory_only": settings.a4_gate == "advisory_only",
            "no_order_placement": True,
            "timeout_seconds": settings.upstream_timeout,
            "idempotent_methods": sorted(IDEMPOTENT),
        },
    }


def _cap_desc(method: str) -> str:
    return {
        "agent.ping": "連線測試，回 pong 與版本",
        "status.get": "回 A5 與上游的連線狀態",
        "news.query": "代為查詢 A1 新聞（快取或轉發）",
        "political.query": "代為查詢 A2 政要表態",
        "brief.get": "生成一份晨報（同步，較慢）",
        "memory.recall": "檢索 A5 的三層記憶",
        "kb.search": "檢索向量知識庫（來源目錄／術語／歷史晨報）",
    }.get(method, "")
