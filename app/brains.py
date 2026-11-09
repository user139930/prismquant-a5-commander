# -*- coding: utf-8 -*-
"""大腦拓撲（Brain Topology）：回答隊友問的「同一個大腦好，還是不同大腦」。

四種拓撲，可切換、可對照：

    rule   不用大模型。規則引擎產出全部結論，零成本、可重現，作為降級與基準線。
    shared 一個大腦。所有角色共用同一模型、同一溫度，角色差異只寫在 prompt 裡。
           優點：一致、便宜、好審計。缺點：同一個模型的偏誤會貫穿整條鏈。
    lens   一個大腦 + 分角色視角與溫度（預設）。感知層 temperature=0 做抽取，
           指揮層 temperature=0.2 做判斷。同一模型版本，語氣統一，但職責分離。
    split  分層大腦。A1/A2 用快模型抽取，A5 用強模型判斷，A4 只准規則。
           優點：貴的算力只花在判斷上，且能降低同源偏誤。缺點：格式與語氣要對齊。

我們預設 lens，理由寫在 docs/BRAIN-TOPOLOGY.md：200 個訂閱者的成本上限、
以及「同一模型版本才能重播同一個決策」的審計要求。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import settings

PROMPT_DIR = Path(__file__).parent / "prompts"

TOPOLOGY_INFO = {
    "rule": {
        "label": "規則大腦",
        "tagline": "不呼叫大模型，全部由規則引擎產出",
        "pros": ["零成本、毫秒級", "完全可重現，適合回歸測試", "沒有 key、沒有網絡也能示範"],
        "cons": ["表達生硬，不會寫得好看", "遇到新主題只能落回宏觀桶"],
        "use_when": "離線評測、降級、成本敏感時段",
    },
    "shared": {
        "label": "共用大腦",
        "tagline": "A1 / A2 / A5 用同一個模型、同一個溫度",
        "pros": ["一致：同一版本模型，輸出風格統一", "便宜：只需維護一個模型設定", "審計簡單：一條鏈只有一個模型版本"],
        "cons": ["同源偏誤：抽取與判斷用同一視角，容易自我確認", "角色隔離只靠 prompt，容易和稀泥"],
        "use_when": "早期打通鏈路、或需要極低成本時",
    },
    "lens": {
        "label": "單腦多視角（預設）",
        "tagline": "同一個模型，分角色溫度與視角：抽取 0.0、判斷 0.2",
        "pros": ["一致性與成本的最佳平衡", "重播性強：模型版本不變，決策可追", "角色邊界清楚，方便寫進報告"],
        "cons": ["模型本身的知識盲區仍然貫穿全鏈", "極端事件下判斷力受限於單一模型"],
        "use_when": "本專案預設：200 訂閱者、要可審計、要控制 LLM 成本",
    },
    "split": {
        "label": "分層大腦",
        "tagline": "感知層用快模型，指揮層用強模型，風控只准規則",
        "pros": ["把貴算力集中在判斷層", "降低同源偏誤：抽取與判斷不同模型", "風控不被模型漂移影響"],
        "cons": ["格式與語氣要額外對齊", "延遲與成本上升", "多一個模型版本要記錄"],
        "use_when": "接入 A3 / A4 後，或 demo 需要展示對照實驗時",
    },
}


@dataclass
class Role:
    name: str
    model: str
    temperature: float
    lens: str


@dataclass
class Plan:
    topology: str
    roles: dict[str, Role] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return TOPOLOGY_INFO.get(self.topology, {}).get("label", self.topology)

    def role(self, key: str) -> Role | None:
        return self.roles.get(key)

    def as_dict(self) -> dict:
        return {
            "topology": self.topology,
            "label": self.label,
            "tagline": TOPOLOGY_INFO.get(self.topology, {}).get("tagline", ""),
            "roles": {k: {"model": r.model, "temperature": r.temperature, "lens": r.lens}
                      for k, r in self.roles.items()},
            "llm_enabled": settings.llm_enabled,
            "prompt_variant": settings.prompt_variant,
        }


LENS = {
    "a1": "你是 A1 感知層：只做抽取與標籤，不預測價格，不補充原文沒有的資訊。",
    "a2": "你是 A2 感知層：只做實體與資產影響映射，保留原始連結與時間。",
    "a5": "你是 A5 指揮層：匯總、調解分歧、做二次分析。必須引用證據 ID，不能發明事件，"
          "不能給保證性表述，不能繞過 A4。",
}


def plan(topology: str | None = None) -> Plan:
    t = (topology or settings.brain_topology or "lens").lower()
    if t not in TOPOLOGY_INFO:
        t = "lens"
    if t == "rule":
        return Plan(t)

    shared = settings.brain_shared_model
    if t == "shared":
        roles = {
            "a1": Role("A1", shared, settings.brain_a5_temperature, LENS["a1"]),
            "a2": Role("A2", shared, settings.brain_a5_temperature, LENS["a2"]),
            "a5": Role("A5", shared, settings.brain_a5_temperature, LENS["a5"]),
        }
    elif t == "split":
        roles = {
            "a1": Role("A1", settings.brain_a1_model, settings.brain_sense_temperature, LENS["a1"]),
            "a2": Role("A2", settings.brain_a2_model, settings.brain_sense_temperature, LENS["a2"]),
            "a5": Role("A5", settings.brain_a5_model, settings.brain_a5_temperature, LENS["a5"]),
        }
    else:  # lens
        m = settings.brain_shared_model
        roles = {
            "a1": Role("A1", m, settings.brain_sense_temperature, LENS["a1"]),
            "a2": Role("A2", m, settings.brain_sense_temperature, LENS["a2"]),
            "a5": Role("A5", m, settings.brain_a5_temperature, LENS["a5"]),
        }
    return Plan(t, roles)


class LLMError(RuntimeError):
    pass


def _endpoint() -> str:
    return settings.llm_base_url.rstrip("/") + "/chat/completions"


def chat_json(system: str, user: str, role: Role, max_tokens: int = 1600) -> tuple[Any, dict]:
    """回傳 (json, meta)。任何失敗都拋 LLMError，由呼叫方降級到規則層。"""
    if not settings.llm_api_key:
        raise LLMError("未設定 PQ_LLM_API_KEY")

    body = {
        "model": role.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": role.temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    started = time.time()
    try:
        with httpx.Client(timeout=settings.llm_timeout) as client:
            resp = client.post(
                _endpoint(), json=body,
                headers={"Authorization": "Bearer %s" % settings.llm_api_key,
                         "HTTP-Referer": "https://github.com/user139930/prismquant-a5-commander",
                         "X-Title": "PrismQuant A5 Commander"},
            )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise LLMError("%s: %s" % (type(exc).__name__, str(exc)[:200]))

    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    usage = data.get("usage") or {}
    return _parse_json(text), {
        "model": role.model,
        "temperature": role.temperature,
        "latency_ms": int((time.time() - started) * 1000),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }


def _parse_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0] or [-1])
    if start < 0:
        raise LLMError("模型沒有輸出 JSON")
    end = max(text.rfind("}"), text.rfind("]"))
    try:
        return json.loads(text[start:end + 1])
    except Exception as exc:
        raise LLMError("JSON 解析失敗：%s" % exc)


# ---------------------------------------------------------------- 提示詞

def load_prompt(name: str) -> str:
    """提示詞按 PQ_PROMPT_VARIANT 版本化，之後要「訓練提示詞」只加版本，不動主流程。"""
    path = PROMPT_DIR / settings.prompt_variant / ("%s.md" % name)
    if not path.exists():
        path = PROMPT_DIR / "v1" / ("%s.md" % name)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def prompt_manifest() -> list[dict]:
    root = PROMPT_DIR
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        files = sorted(f.name for f in d.glob("*.md"))
        out.append({"variant": d.name, "files": files, "active": d.name == settings.prompt_variant})
    return out
