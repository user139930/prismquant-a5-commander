# -*- coding: utf-8 -*-
"""A5 指揮官智能體：FastAPI 服務。

接口：
    GET  /health
    POST /api/v1/commander/run   body: {"question": "..."}
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app import commander
from app.config import settings

DEMO_PAGE = (Path(__file__).parent / "demo.html").read_text(encoding="utf-8")

app = FastAPI(
    title=settings.service_name,
    version="0.1.0",
    description="PrismQuant A5 指揮官智能體：匯總 A1 新聞與 A2 政要監控，產出晨報與證據鏈。",
    docs_url=None,
    redoc_url=None,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


class RunRequest(BaseModel):
    question: str = ""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return DEMO_PAGE


@app.get("/docs", include_in_schema=False)
@app.get("/redoc", include_in_schema=False)
def _redirect_docs():
    return RedirectResponse(url="/")


@app.get("/health")
def health():
    return {"status": "ok", "service": settings.service_name, "version": app.version, "scope": "api_process"}


@app.post("/api/v1/commander/run")
def run(req: RunRequest):
    try:
        return commander.run(req.question)
    except Exception as exc:
        raise HTTPException(503, detail={"code": "commander_failed", "message": str(exc)})
