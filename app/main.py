# -*- coding: utf-8 -*-
"""A5 指揮官：FastAPI 服務 + 單頁控制台。

    GET  /                          控制台（晨報 / 探針 / 大腦 / 運行記錄 / 合規）
    GET  /health
    GET  /api/v1/upstream/health    A1 / A2 / A4 / A5 狀態
    POST /api/v1/commander/run      {"question": "...", "topology": "lens"}
    POST /api/v1/commander/probe    {"title": "...", "text": "...", "source": "...", "url": "..."}
    GET  /api/v1/runs               運行記錄
    GET  /api/v1/runs/{run_id}      單次詳情
    POST /api/v1/runs/{run_id}/replay  用同一份輸入重跑，比對漂移
    GET  /api/v1/brain              大腦拓撲與對比
    POST /api/v1/brain/compare      兩種拓撲跑同一份輸入（需要 LLM key）
    GET  /api/v1/compliance         護欄配置與免責
    POST /api/v1/telegram/preview / send
    POST /api/v1/telegram/webhook   Telegram 指令通道
"""
from __future__ import annotations

import json
import queue
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import (brains, bus, commander, connectors, guardrails, ledger, memory,
                 promptkit, rag, scheduler, store, telegram, tools)
from app.compliance import Compliance
from app.config import settings

STATIC = Path(__file__).parent / "static"


def _register_bus_methods() -> None:
    """A5 對外開放的方法。隊友的代理（或 curl）打 /api/v1/a2a/inbox 就是走這裡。

    刻意只開放讀取與生成，不開放任何寫入倉位、修改風控閾值的入口。
    """
    bus.register("agent.ping", lambda p: {
        "pong": True, "agent": "A5", "version": "1.0.0",
        "prompt": settings.prompt_variant, "topology": settings.brain_topology})

    bus.register("status.get", lambda p: {
        "upstream": {k: {"ok": v.get("ok"), "latency_ms": v.get("latency_ms")}
                     for k, v in connectors.health().items()},
        "rag": rag.stats().get("chunks"),
        "memory": memory.stats().get("total"),
    })

    def _news(p: dict) -> dict:
        events, meta = connectors.a1_events(int(p.get("limit") or settings.upstream_limit_a1))
        return {"online": bool(meta.get("ok")), "count": len(events),
                "events": events[: int(p.get("limit") or 10)]}

    def _political(p: dict) -> dict:
        events, meta = connectors.a2_events(int(p.get("limit") or settings.upstream_limit_a2))
        return {"online": bool(meta.get("ok")), "degraded": meta.get("degraded"),
                "count": len(events), "events": events[: int(p.get("limit") or 10)]}

    def _brief(p: dict) -> dict:
        payload = commander.run(p.get("question") or "生成今天的晨報")
        return {"run_id": payload.get("run_id"), "headline": payload.get("headline"),
                "confidence": (payload.get("confidence") or {}).get("score"),
                "url": "%s/#brief/%s" % (settings.web_base_url.rstrip("/"),
                                         payload.get("run_id"))}

    bus.register("news.query", _news)
    bus.register("political.query", _political)
    bus.register("brief.get", _brief)
    bus.register("memory.recall", lambda p: memory.recall(
        p.get("query") or "", k=int(p.get("k") or 5), layer=p.get("layer") or None))
    bus.register("kb.search", lambda p: rag.search(
        p.get("query") or "", k=int(p.get("k") or 5), kind=p.get("kind") or None))


@asynccontextmanager
async def lifespan(_: FastAPI):
    ledger.init()
    bus.init()
    memory.init()
    rag.init()
    tools.init()
    _register_bus_methods()
    if settings.rag_seed_on_start:
        try:
            rag.ensure_seed()
        except Exception:
            pass                      # 種子匯入失敗不該讓服務起不來
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(
    title=settings.service_name,
    version="1.0.0",
    description="PrismQuant A5 指揮官：A1 + A2 匯總、二次分析、A2A 通信、三層記憶、RAG 知識庫、"
                "工具註冊表、提示詞分層與合規護欄。",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ---------------------------------------------------------------- 請求模型

class RunRequest(BaseModel):
    question: str = ""
    topology: str | None = Field(default=None, description="rule / shared / lens / split")


class ProbeRequest(BaseModel):
    title: str = ""
    text: str
    source: str = ""
    url: str = ""


class GuardRequest(BaseModel):
    """外部系統（例如 Winter）呼叫合規檢查的入參。"""
    text: str
    evidence_ids: list[str] = []      # 呼叫方現有的證據編號；空 = 跳過引用校驗
    a4_connected: bool = False        # A4 接上後傳 true，才會允許明確倉位


class StanceRequest(BaseModel):
    """使用者對某組分歧的裁決。存進語義記憶，下次同主題會先看它。"""
    topic: str
    side: str = "a1"          # a1 / a2 / neutral
    note: str = ""
    run_id: str = ""


class CompareRequest(BaseModel):
    question: str = "生成今天的晨報"
    topologies: list[str] = Field(default_factory=lambda: ["lens", "split"])


class WatchlistRequest(BaseModel):
    terms: list[str] = Field(default_factory=list)


class LlmSettingsRequest(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class GateRequest(BaseModel):
    run_id: str


class AskRequest(BaseModel):
    run_id: str
    question: str


class RenderRequest(BaseModel):
    payload: dict


class MemoryWriteRequest(BaseModel):
    text: str
    layer: str = Field(default="episodic", description="episodic / semantic")
    kind: str = "note"
    key: str = ""
    importance: float = 0.6


class RagIngestRequest(BaseModel):
    title: str
    text: str
    kind: str = "note"
    source: str = ""
    url: str = ""


class ToolCallRequest(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)
    trace_id: str = ""


class BusSendRequest(BaseModel):
    recipient: str = "A5"
    method: str = "agent.ping"
    payload: dict = Field(default_factory=dict)
    trace_id: str = ""


# ---------------------------------------------------------------- 頁面

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": settings.service_name,
        "version": app.version,
        "scope": "api_process",
        "brain": settings.brain_topology,
        "prompt": settings.prompt_variant,
        "llm": settings.llm_enabled,
    }


@app.get("/api/v1/meta")
def meta():
    bs = bus.stats()
    ms = memory.stats()
    rs = rag.stats()
    return {
        "service": settings.service_name,
        "version": app.version,
        "brain": settings.brain_topology,
        "prompt_variant": settings.prompt_variant,
        "llm_enabled": settings.llm_enabled,
        "a4_gate": settings.a4_gate,
        "watchlist": settings.watchlist,
        "brief_target_max_minutes": settings.brief_target_max_minutes,
        "prompts": brains.prompt_manifest(),
        "infra": {
            "bus": {"messages": bs["messages"], "traces": bs["traces"],
                    "methods": bs["methods"], "card": "/.well-known/agent-card.json"},
            "memory": {k: v["count"] for k, v in ms["by_layer"].items()},
            "rag": {"docs": rs["docs"], "chunks": rs["chunks"],
                    "backend": rs["embed_backend"]},
            "tools": tools.catalog()["count"],
            "self_check": "GET /api/v1/arch",
        },
        "endpoints": [
            "GET /api/v1/upstream/health",
            "POST /api/v1/commander/run",
            "POST /api/v1/commander/probe",
            "GET /api/v1/runs",
            "GET /api/v1/runs/{run_id}",
            "POST /api/v1/runs/{run_id}/replay",
            "GET /api/v1/brain",
            "POST /api/v1/brain/compare",
            "GET /api/v1/compliance",
            "GET /.well-known/agent-card.json",
            "POST /api/v1/a2a/inbox",
            "GET /api/v1/bus/messages",
            "GET /api/v1/bus/trace/{trace_id}",
            "GET /api/v1/memory",
            "GET /api/v1/memory/search",
            "GET /api/v1/rag/search",
            "GET /api/v1/tools",
            "POST /api/v1/tools/call",
            "GET /api/v1/prompts",
            "GET /api/v1/arch",
        ],
    }


# ---------------------------------------------------------------- 上游

@app.get("/api/v1/upstream/health")
def upstream_health():
    h = connectors.health()
    h["ledger"] = ledger.stats()
    return h


# ---------------------------------------------------------------- 指揮官

@app.post("/api/v1/commander/run")
def run(req: RunRequest):
    try:
        return commander.run(req.question, req.topology)
    except Exception as exc:  # 不讓單次失敗把服務打掛
        raise HTTPException(503, detail={"code": "commander_failed", "message": str(exc)[:300]})


@app.get("/api/v1/commander/stream")
def run_stream(question: str = "", topology: str | None = None):
    """SSE 版晨報：把「取上游 → 規則層 → 大模型 → 護欄」的進度即時推給前端。

    大模型一次要 30–60 秒，普通 POST 會讓畫面死住；串流之後使用者看得到每個 agent 在做什麼。
    """
    def gen():
        q: queue.Queue = queue.Queue()
        box: dict = {}

        def worker():
            try:
                box["p"] = commander.run(question, topology, progress=q.put)
            except Exception as exc:
                q.put({"stage": "error", "message": str(exc)[:300], "error": True})
            finally:
                q.put(None)

        threading.Thread(target=worker, daemon=True).start()
        while True:
            event = q.get()
            if event is None:
                break
            yield "data: " + json.dumps({"type": "progress", **event}, ensure_ascii=False) + "\n\n"
        if "p" in box:
            yield "data: " + json.dumps({"type": "result", "payload": box["p"]}, ensure_ascii=False) + "\n\n"
        else:
            yield "data: " + json.dumps({"type": "error", "message": "生成失敗，請看伺服器日誌"},
                                        ensure_ascii=False) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/v1/commander/ask")
def ask(req: AskRequest):
    """對某一份晨報追問。這是 proposal F-07「接受用戶指令」的實作。"""
    row = ledger.get(req.run_id)
    if row is None:
        raise HTTPException(404, detail={"code": "run_not_found", "message": "找不到這份晨報"})
    if not (req.question or "").strip():
        raise HTTPException(422, detail={"code": "empty_question", "message": "問題不能為空"})
    try:
        return commander.ask(row["payload"], req.question)
    except Exception as exc:
        raise HTTPException(503, detail={"code": "ask_failed", "message": str(exc)[:300]})


@app.get("/api/v1/commander/compare")
def compare(a: str, b: str):
    try:
        return commander.compare_runs(a, b)
    except KeyError:
        raise HTTPException(404, detail={"code": "run_not_found", "message": "找不到其中一份晨報"})
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "not_comparable", "message": str(exc)})


@app.get("/api/v1/integration")
def integration(probe: bool = False):
    """五代理的整合狀態：誰已接入、誰缺席、各自要交什麼契約。

    這個頁面是給隊友看的：A5 沒有假裝自己有 A1–A4，缺的部分寫清楚等誰。
    """
    h = connectors.health()
    agents = []
    for aid in ("A1", "A2", "A3", "A4", "A5"):
        meta = commander.AGENT_META[aid]
        if aid == "A5":
            status, note = "ok", "已交付（本服務）"
        elif aid in ("A1", "A2"):
            x = h[aid.lower()]
            status = "ok" if x.get("ok") else "offline"
            note = ("已連線 · %d ms" % (x.get("latency_ms") or 0)) if x.get("ok") \
                else "未連線：%s" % (x.get("error") or "沒有回應")
        elif aid == "A3":
            x = h.get("a3") or {}
            if x.get("ok"):
                intents, m3 = connectors.a3_intents(5)
                status = "ok"
                note = "已連線 · %d 條執行意圖" % len(intents)
            else:
                status = "missing"
                note = "未接入：等 GET /api/v1/intents（A5 的執行意圖卡留空）"
        else:
            status, note = "missing", "未實作，A5 相關區塊留空"
        agents.append({"id": aid, **meta, "status": status, "note": note})

    sample = None
    if probe:
        a1, m1 = connectors.a1_events(3)
        a2, m2 = connectors.a2_events(3)
        sample = {
            "a1": {"meta": m1, "items": a1[:2]},
            "a2": {"meta": m2, "items": a2[:2]},
        }
    return {
        "agents": agents,
        "health": h,
        "sample": sample,
        "ledger": ledger.stats(),
        "sample_data_policy": "A1/A2 離線時整份改用內建樣本；分析、大模型、合規檢查照常執行，樣本條目標記 sampled。",
    }


@app.get("/api/v1/schedule")
def schedule_status():
    return scheduler.status()


@app.post("/api/v1/schedule/run")
async def schedule_run():
    """手動觸發一次排程流程（生成 + 推送），用來驗證排程是否正常。"""
    return await scheduler.fire("manual")


@app.post("/api/v1/commander/gate")
def gate(req: GateRequest):
    """模擬 A4 風控閘門：把晨報的傳導鏈當成假設的執行意圖，跑一遍規則。

    明確標記 simulated——A4 還沒有人做，這裡只是把「風控會怎麼否決」演出來，
    不是真的下單，也不是真的風控。
    """
    row = ledger.get(req.run_id)
    if row is None:
        raise HTTPException(404, detail={"code": "run_not_found", "message": "找不到這份晨報"})
    if row["kind"] != "brief":
        raise HTTPException(422, detail={"code": "not_a_brief", "message": "只有晨報可以模擬風控"})
    try:
        return commander.simulate_gate(row["payload"])
    except Exception as exc:
        raise HTTPException(503, detail={"code": "gate_failed", "message": str(exc)[:300]})


@app.get("/api/v1/commander/diff/{run_id}")
def diff(run_id: str):
    try:
        return commander.diff_with_previous(run_id)
    except KeyError:
        raise HTTPException(404, detail={"code": "run_not_found", "message": "找不到這次 run"})


@app.post("/api/v1/commander/probe")
def probe(req: ProbeRequest):
    if not (req.text or req.title).strip():
        raise HTTPException(422, detail={"code": "empty_input", "message": "請至少貼標題或正文"})
    try:
        return commander.probe(req.title, req.text, req.source, req.url)
    except Exception as exc:
        raise HTTPException(503, detail={"code": "probe_failed", "message": str(exc)[:300]})


# ---------------------------------------------------------------- 運行記錄

@app.get("/api/v1/runs")
def runs(limit: int = 30, kind: str | None = None, q: str | None = None):
    fetch = 200 if (kind or q) else limit
    items = ledger.list_runs(fetch)
    if kind and kind not in ("all", ""):
        items = [r for r in items if r["kind"] == kind]
    if q:
        needle = q.lower()
        items = [r for r in items if needle in (r.get("question") or "").lower()]
    return {"items": items[:limit], "stats": ledger.stats()}


@app.get("/api/v1/runs/{run_id}")
def run_detail(run_id: str):
    row = ledger.get(run_id)
    if row is None:
        raise HTTPException(404, detail={"code": "run_not_found", "message": "找不到這次 run"})
    return row


@app.post("/api/v1/runs/{run_id}/replay")
def run_replay(run_id: str):
    row = ledger.get(run_id)
    if row is None:
        raise HTTPException(404, detail={"code": "run_not_found", "message": "找不到這次 run"})
    snap = row.get("snapshot") or {}
    started = time.time()

    if row["kind"] == "probe":
        new = commander.probe(snap.get("title", ""), snap.get("text", ""),
                              snap.get("source", ""), snap.get("url", ""))
    else:
        new = commander.run(snap.get("question", ""), snap.get("topology"))

    old = row["payload"]
    old_conf = (old.get("confidence") or {}).get("score", 0)
    new_conf = (new.get("confidence") or {}).get("score", 0)
    old_clusters = [c["label"] for c in old.get("clusters", [])]
    new_clusters = [c["label"] for c in new.get("clusters", [])]

    return {
        "run_id": run_id,
        "replay_run_id": new.get("run_id"),
        "elapsed_ms": int((time.time() - started) * 1000),
        "drift": {
            "confidence_delta": round(new_conf - old_conf, 3),
            "conflict_delta": len(new.get("conflicts", [])) - len(old.get("conflicts", [])),
            "evidence_delta": len(new.get("evidence", [])) - len(old.get("evidence", [])),
            "top_cluster_changed": (old_clusters[:1] != new_clusters[:1]),
            "headline_changed": old.get("headline") != new.get("headline"),
        },
        "original": {"headline": old.get("headline"), "confidence": old_conf,
                     "conflicts": len(old.get("conflicts", [])), "run_id": run_id},
        "current": new,
    }


# ---------------------------------------------------------------- 大腦

@app.get("/api/v1/brain")
def brain_info():
    configured = brains.plan().as_dict()
    return {
        "configured": configured,
        "plans": {t: brains.plan(t).as_dict() for t in ("rule", "shared", "lens", "split")},
        "topologies": [
            {"key": k, **v} for k, v in brains.TOPOLOGY_INFO.items()
        ],
        "comparison_table": [
            {"dim": "成本", "shared": "低（一個模型）", "lens": "低", "split": "中（貴算力只用在判斷層）"},
            {"dim": "一致性", "shared": "高", "lens": "高", "split": "中（要對齊格式）"},
            {"dim": "同源偏誤", "shared": "高（抽取與判斷同一視角）", "lens": "高", "split": "低"},
            {"dim": "可重現／可審計", "shared": "好", "lens": "最好（模型版本固定）", "split": "較差（多個版本）"},
            {"dim": "延遲", "shared": "低", "lens": "低", "split": "中高"},
            {"dim": "適合階段", "shared": "打通鏈路", "lens": "本專案預設", "split": "接入 A3/A4 後"},
        ],
        "recommendation": {
            "choice": "lens",
            "why": [
                "A5 的價值在「可審計的解釋」，不是模型多樣性：同一模型版本才能重播同一個決策。",
                "200 個訂閱者的成本上限：判斷層只跑一次，不需要三個模型各跑一次。",
                "風控不能漂移：A4 接入後只准規則與統計模型放行，模型只負責解釋，所以不需要為風控配一個大腦。",
                "但要保留 split：等 A3/A4 上線、或要做對照實驗時，一個環境變數就能切。",
            ],
            "trade_off": "lens 的最大風險是單一模型的知識盲區會貫穿全鏈，靠「交叉驗證 + 置信度扣分 + 單一來源降級」來補，而不是靠多買一個模型。",
        },
        "llm_enabled": settings.llm_enabled,
    }


@app.post("/api/v1/brain/compare")
def brain_compare(req: CompareRequest):
    if not settings.llm_api_key:
        raise HTTPException(409, detail={
            "code": "llm_required",
            "message": "對照實驗需要 PQ_LLM_API_KEY；沒有 key 時請看靜態對比表，或先用 rule 模式驗證規則層。",
        })
    tops = [t for t in req.topologies if t in {"shared", "lens", "split"}][:2]
    if len(tops) < 2:
        raise HTTPException(422, detail={"code": "need_two", "message": "請選兩個不同的 LLM 拓撲"})

    out = []
    for t in tops:
        payload = commander.run(req.question, t)
        out.append({
            "topology": t,
            "label": payload["brain"]["label"],
            "headline": payload["headline"],
            "confidence": payload["confidence"]["score"],
            "citations": payload["quality_flags"]["citation_count"],
            "conflicts": len(payload["conflicts"]),
            "chars": len(payload.get("brief_text", "")),
            "latency_ms": payload["stats"]["latency_ms"],
            "tokens": payload["stats"]["tokens"],
            "degraded": payload["brain"]["degraded"],
            "run_id": payload["run_id"],
        })
    return {"question": req.question, "results": out}


# ---------------------------------------------------------------- 智能體基建（v1.0）

@app.get("/.well-known/agent-card.json", include_in_schema=False)
def agent_card():
    """A2A 能力發現：隊友的代理先讀這張卡，才知道能叫 A5 做什麼。"""
    return bus.card()


@app.post("/api/v1/a2a/inbox")
async def a2a_inbox(request: Request):
    """收件匣。接受 A2A 信封，也接受 JSON-RPC 2.0（curl 順手）。

    隊友用法：
        curl -X POST http://127.0.0.1:8130/api/v1/a2a/inbox \\
             -H 'Content-Type: application/json' \\
             -d '{"jsonrpc":"2.0","id":1,"method":"agent.ping","params":{}}'
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={
            "code": "bad_json", "message": "信封必須是合法 JSON"})
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={
            "code": "bad_envelope", "message": "信封必須是 JSON 物件"})
    return bus.handle_incoming(body)


@app.get("/api/v1/bus/messages")
def bus_messages(limit: int = 30, agent: str | None = None):
    return {"stats": bus.stats(), "messages": bus.messages(limit=limit, agent=agent)}


@app.get("/api/v1/bus/trace/{trace_id}")
def bus_trace(trace_id: str):
    return bus.trace(trace_id)


@app.post("/api/v1/bus/send")
def bus_send(req: BusSendRequest):
    msg = bus.request(req.recipient, req.method, req.payload,
                      trace_id=req.trace_id, sender="user")
    return msg.model_dump()


# ---------------------------------------------------------------- 記憶

@app.get("/api/v1/winter/review")
def winter_review(limit: int = 6):
    """旁路審查 Winter 的輸出：拉它的消息，清洗一遍，回傳「原文 vs 審查後」。

    不改對方任何代碼，也不寫它的資料庫——只讀 HTTP，把差異擺出來。
    對方沒開（8766 連不上）就老實說連不上，不假裝有結果。
    """
    base = settings.winter_api_url.rstrip("/")
    try:
        with httpx.Client(timeout=4, trust_env=False) as client:
            convs = client.get(base + "/api/v1/conversations").json()
            if not isinstance(convs, list):
                raise ValueError("會話列表格式不符")
            convs = convs[:3]
            out = []
            for c in convs:
                cid = c.get("id")
                if not cid:
                    continue
                msgs = client.get(base + "/api/v1/conversations/%s/messages" % cid).json()
                rows = []
                for m in (msgs if isinstance(msgs, list) else []):
                    raw = m.get("content") or ""
                    if not raw.strip():
                        continue
                    verdict = Compliance().apply(raw, check_position=True)
                    rows.append({
                        "id": m.get("id"),
                        "agent": m.get("agent") or "",
                        "created_at": m.get("created_at") or "",
                        "original": raw,
                        "reviewed": verdict["text"],
                        "changed": verdict["flagged"],
                        "blocked": verdict["blocked"],
                        "broken": verdict["broken"],
                    })
                out.append({"id": cid, "title": c.get("title") or "（無標題）",
                            "updated_at": c.get("updated_at") or "",
                            "messages": rows[-limit:]})
    except Exception as exc:
        return {"ok": False, "winter": base,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:120]),
                "hint": "先啟動 Winter（winter.cmd，8766），或在 .env 改 PQ_WINTER_API_URL"}

    changed = sum(1 for c in out for m in c["messages"] if m["changed"])
    total = sum(len(c["messages"]) for c in out)
    return {
        "ok": True,
        "winter": base,
        "conversations": out,
        "summary": {
            "messages": total,
            "changed": changed,
            "blocked": sum(len(m["blocked"]) for c in out for m in c["messages"]),
            "broken": sum(len(m["broken"]) for c in out for m in c["messages"]),
        },
    }


@app.post("/api/v1/guard")
def guard_text(req: GuardRequest):
    """給外部系統用的合規檢查：送一段文字，回傳清洗後的版本。

    為什麼要有這個端點：隊友的 Winter 有自己的 A5（調度器），不該把兩套代碼合併。
    正確的整合是服務調服務——他在寫入訊息前 POST 一次這裡，拿到乾淨的文本繼續。
    合約見 docs/GUARD-API.md。
    """
    g = Compliance(a4_connected=bool(req.a4_connected))
    verdict = g.apply(req.text, allowed_ids=req.evidence_ids or None)
    return {
        "text": verdict["text"],
        "passed": verdict["ok"],
        "flagged": verdict["flagged"],
        "blocked": verdict["blocked"],
        "broken": verdict["broken"],
        "notes": verdict["notes"],
        "disclaimer": verdict["disclaimer"],
        "advisory_only": verdict["advisory_only"],
    }


@app.post("/api/v1/stance")
def set_stance(req: StanceRequest):
    """記錄使用者在某組分歧上的立場。

    為什麼不放進 brief payload 而是寫記憶：立場要跨晨報存活，
    而且它會影響下一次的裁決敘述（「你上次站政策面」）。
    """
    label = {"a1": "新聞面", "a2": "政策面", "neutral": "兩邊都不站"}.get(req.side, req.side)
    topic = (req.topic or "").strip()
    if not topic:
        raise HTTPException(422, detail={"code": "bad_topic", "message": "缺少 topic"})
    text = "%s：使用者傾向 %s" % (topic, label)
    if req.note:
        text += "（%s）" % req.note.strip()[:60]
    r = memory.remember(text=text, layer="semantic", kind="stance",
                        key="stance:" + topic, importance=0.75,
                        source_run=req.run_id,
                        meta={"topic": topic, "side": req.side, "label": label})
    return {"ok": bool(r.get("ok")), "label": label, "id": r.get("id"),
            "times": r.get("hits", 1), "note": "已記錄，下次同主題會先看你的傾向"}


@app.get("/api/v1/memory")
def memory_list(layer: str | None = None, limit: int = 40):
    return {"stats": memory.stats(), "items": memory.list_items(layer=layer, limit=limit)}


@app.get("/api/v1/memory/search")
def memory_search(q: str = "", k: int = 5, layer: str | None = None):
    memory.decay()
    return memory.recall(q, k=k, layer=layer)


@app.post("/api/v1/memory")
def memory_add(req: MemoryWriteRequest):
    return memory.remember(req.text, layer=req.layer, kind=req.kind, key=req.key,
                           importance=req.importance)


@app.delete("/api/v1/memory/{item_id}")
def memory_delete(item_id: str, hard: bool = False):
    if not memory.forget(item_id, hard=hard):
        raise HTTPException(status_code=404, detail={
            "code": "not_found", "message": "找不到這條記憶：%s" % item_id})
    return {"ok": True, "id": item_id, "hard": hard,
            "note": "硬刪除後不可恢復；軟刪除（archived=2）不再參與檢索"}


# ---------------------------------------------------------------- 向量知識庫

@app.get("/api/v1/rag/stats")
def rag_stats():
    return rag.stats()


@app.get("/api/v1/rag/search")
def rag_search(q: str, k: int = 5, kind: str | None = None, mmr: bool = True):
    return rag.search(q, k=k, kind=kind, mmr=mmr)


@app.post("/api/v1/rag/ingest")
def rag_ingest(req: RagIngestRequest):
    return rag.ingest_document(title=req.title, text=req.text, kind=req.kind,
                               source=req.source, url=req.url)


@app.post("/api/v1/rag/seed")
def rag_seed(with_briefs: bool = True):
    seed = rag.ensure_seed()
    briefs = rag.ingest_latest_briefs(5) if with_briefs else 0
    return {"seed": seed, "briefs": briefs, "stats": rag.stats()}


# ---------------------------------------------------------------- 工具註冊表

@app.get("/api/v1/tools")
def tools_list(openai: bool = False):
    return {"catalog": tools.catalog(),
            "specs": tools.specs(openai=True) if openai else [],
            "audit": tools.audit_stats(),
            "recent": tools.audit(10)}


@app.post("/api/v1/tools/call")
def tools_call(req: ToolCallRequest):
    return tools.call(req.name, req.args, trace_id=req.trace_id)


@app.get("/api/v1/tools/audit")
def tools_audit(limit: int = 30):
    return {"stats": tools.audit_stats(), "items": tools.audit(limit)}


# ---------------------------------------------------------------- 提示詞

@app.get("/api/v1/prompts")
def prompts_list():
    return promptkit.manifest()


@app.get("/api/v1/prompts/{name}")
def prompts_show(name: str, ver: str | None = None):
    d = promptkit.show(name, ver)
    if not d["raw"]:
        raise HTTPException(status_code=404, detail={
            "code": "not_found", "message": "找不到提示詞：%s" % name})
    return d


# ---------------------------------------------------------------- 五問自檢

@app.get("/api/v1/arch")
def arch():
    return commander.infra()


# ---------------------------------------------------------------- 合規 / Telegram

@app.get("/api/v1/settings/llm")
def get_llm_settings():
    return store.get_llm()


@app.put("/api/v1/settings/llm")
def put_llm_settings(req: LlmSettingsRequest):
    """在介面上貼 key：存本機 data/prefs.json，即時生效，不用重啟。

    回傳永遠是遮罩過的 key——前端拿不到完整字串，截圖或分享畫面都不會漏。
    """
    return store.set_llm(req.api_key, req.base_url, req.model)


@app.post("/api/v1/settings/llm/test")
def test_llm_settings():
    if not settings.llm_api_key:
        raise HTTPException(400, detail={"code": "no_key", "message": "還沒設定 key"})
    if settings.brain_topology == "rule":
        raise HTTPException(400, detail={"code": "rule_mode",
                                         "message": "目前拓撲是 rule，不會呼叫大模型；先切成 lens"})
    role = brains.plan().role("a5")
    try:
        out, meta = brains.chat_json("只輸出 JSON。", '回 {"ok":true}', role, max_tokens=20)
        return {"ok": True, "model": role.model, "latency_ms": meta.get("latency_ms"),
                "reply": out}
    except brains.LLMError as exc:
        raise HTTPException(502, detail={"code": "llm_failed", "message": str(exc)[:200]})


@app.get("/api/v1/watchlist")
def get_watchlist():
    return {"terms": store.get_watchlist(), "defaults": settings.watchlist}


@app.put("/api/v1/watchlist")
def put_watchlist(req: WatchlistRequest):
    return {"terms": store.set_watchlist(req.terms)}


@app.post("/api/v1/watchlist/reset")
def reset_watchlist():
    return {"terms": store.reset_watchlist()}


@app.get("/api/v1/compliance")
def compliance():
    recent = [r for r in ledger.list_runs(40) if r.get("guard_passed") == 0]
    return {
        "a4_gate": settings.a4_gate,
        "a4_connected": bool(settings.a4_api_url),
        "banned_phrases": settings.banned_phrases,
        "guard_recent_blocks": recent,
        "disclaimer": (
            "PrismQuant 為研究與決策輔助工具，不代客理財、不託管資金、不構成投資建議。"
            "所有方向標籤為概率判斷，可能出錯；政治人物表態的市場影響僅為概率標籤，非因果證明。"
        ),
        "data_caveats": [
            "免費新聞來源有缺口與延遲，A1 需多來源交叉驗證。",
            "A2 目前以 RSS 為主，改爬蟲後延遲會下降；A5 的適配層已兼容兩種 schema。",
            "A4 風控未接入：本服務不產生任何下單指令。",
            "大模型沒有價格特權資訊，輸出可能幻覺，因此每條結論必須帶證據 ID。",
        ],
        "sample_mode_policy": "任何上游失敗時整份改用內建樣本並在頁面標示，不真假混用。",
    }


@app.post("/api/v1/telegram/render")
def telegram_render(req: RenderRequest):
    return {"message": telegram.build_message(req.payload)}


def _delivery_text(payload: dict) -> str:
    """對話型 payload 沒有晨報結構，直接送 reply。"""
    if payload.get("kind") == "chat":
        return "%s\n\n%s" % (payload.get("reply", ""), payload.get("context", ""))
    return telegram.build_message(payload)


@app.post("/api/v1/telegram/preview")
def telegram_preview(req: RunRequest):
    payload = commander.run(req.question, req.topology)
    return {"message": _delivery_text(payload), "run": payload}


@app.post("/api/v1/telegram/send")
def telegram_send(req: RunRequest):
    payload = commander.run(req.question, req.topology)
    result = telegram.send(_delivery_text(payload))
    return {"delivery": result, "run_id": payload.get("run_id")}


@app.post("/api/v1/telegram/webhook")
async def telegram_webhook(request: Request):
    if settings.telegram_webhook_secret:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != settings.telegram_webhook_secret:
            raise HTTPException(403, detail={"code": "bad_secret", "message": "secret 不符"})
    update = await request.json()
    message = (update.get("message") or {}).get("text") or ""
    if not message:
        return {"ok": True, "ignored": True}
    reply = telegram.handle_command(message)
    telegram.send(reply)
    return {"ok": True, "reply": reply}


@app.exception_handler(Exception)
def _unhandled(_: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": {"code": "internal_error", "message": str(exc)[:300],
                            "guard": guardrails.scan(str(exc))}},
    )
