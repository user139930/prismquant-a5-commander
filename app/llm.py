# -*- coding: utf-8 -*-
"""呼叫大模型（OpenRouter，OpenAI 相容介面）+ 寬鬆 JSON 解析。"""
import json
import re

from openai import OpenAI

from app.config import settings

_client = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        if not settings.llm_api_key:
            raise RuntimeError("請在 .env 設定 PQ_LLM_API_KEY（OpenRouter key）")
        _client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
    return _client


def chat_json(system: str, user: str, temperature: float = 0.1, max_tokens: int = 1800):
    resp = get_client().chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    text = (resp.choices[0].message.content or "").strip()
    return parse_json(text)


def parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("模型未輸出 JSON：" + text[:200])
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start:end + 1])
