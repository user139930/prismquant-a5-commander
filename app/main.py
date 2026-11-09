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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import brains, commander, connectors, guardrails, ledger, store, telegram
from app.config import settings

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    ledger.init()
    yield


app = FastAPI(
    title=settings.service_name,
    version="0.4.2",
    description="PrismQuant A5 指揮官：A1 + A2 匯總、二次分析、大腦拓撲與合規護欄。",
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


class CompareRequest(BaseModel):
    question: str = "生成今天的晨報"
    topologies: list[str] = Field(default_factory=lambda: ["lens", "split"])


class WatchlistRequest(BaseModel):
    terms: list[str] = Field(default_factory=list)


class AskRequest(BaseModel):
    run_id: str
    question: str


class RenderRequest(BaseModel):
    payload: dict


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
        "sample_data_policy": "A1/A2 離線時整份改用內建示例；分析流程、大模型、合規檢查都是真實執行。",
    }


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


# ---------------------------------------------------------------- 合規 / Telegram

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
        "sample_mode_policy": "任何上游失敗時整份改用示例資料，並在頁面標示，不真假混用。",
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
