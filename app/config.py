# -*- coding: utf-8 -*-
"""A5 設定：環境變數前綴 PQ_，讀取專案根目錄的 .env。

設計原則：
1. 所有可調參數都在這裡，程式碼裡不留魔術數字，方便報告寫「可配置」。
2. 大腦拓撲（brain topology）是 A5 的核心設計決策，用三個獨立 model 欄位表達，
   而不是一個 model 字串——這樣「同一個大腦 / 不同大腦」才是可切換的實作，
   不是文件裡的一句口號。
"""
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="PQ_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    service_name: str = "PrismQuant A5 Commander API"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8130
    cors_origins: list[str] = Field(default_factory=list)

    # ---------- 上游智能體（A1 / A2 已存在，A4 未有） ----------
    a1_api_url: str = "http://127.0.0.1:8010"
    a2_api_url: str = "http://127.0.0.1:8120"
    a4_api_url: str = ""          # A4 風控未接入，留空即為「只有建議權」
    upstream_timeout: float = 6.0
    upstream_limit_a1: int = 40
    upstream_limit_a2: int = 20

    # ---------- 大腦拓撲 ----------
    # rule   : 完全不呼叫大模型（離線、可重現、零成本）
    # shared : 一個大腦，所有角色共用同一模型與溫度
    # lens   : 一個大腦 + 分角色溫度與視角（預設：一致性優先，成本最低）
    # split  : 分層大腦，感知層用快模型、指揮層用強模型、風控只准規則
    brain_topology: str = "lens"
    llm_api_key: str = ""
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout: float = 45.0
    brain_shared_model: str = "deepseek/deepseek-chat"
    brain_a1_model: str = "deepseek/deepseek-chat"     # 感知層：抽取、標籤
    brain_a2_model: str = "deepseek/deepseek-chat"     # 感知層：實體、影響
    brain_a5_model: str = "deepseek/deepseek-chat"     # 指揮層：調解、推演
    brain_a5_temperature: float = 0.2
    brain_sense_temperature: float = 0.0
    llm_max_tokens: int = 1200

    # 成本估算（USD / 1M tokens）。預設值是 deepseek-chat 在 OpenRouter 的牌價量級，
    # 請按實際賬單調整；設成 0 就只顯示 tokens、不顯示金額。
    price_in_per_m: float = 0.27
    price_out_per_m: float = 1.10

    # ---------- 提示詞版本（之後要「訓練提示詞」就從這裡擴） ----------
    prompt_variant: str = "v1"

    # ---------- 合規護欄 ----------
    a4_gate: str = "advisory_only"        # advisory_only | risk_checked
    banned_phrases: list[str] = [
        "必漲", "必跌", "穩賺", "包賺", "保證盈利", "零風險", "翻倍",
        "一定會", "肯定會漲", "肯定會跌", "內幕消息", "梭哈", "all in", "all-in",
        "立即買入", "立即賣出", "全倉",
    ]

    # ---------- 關注關鍵詞（A5 自己的策略偏好，用來重排上游訊號） ----------
    watchlist: list[str] = [
        "關稅", "利率", "降息", "加息", "半導體", "芯片", "能源", "原油",
        "監管", "地緣", "人民幣", "港股", "加密", "穩定幣",
    ]

    # ---------- 定時晨報 ----------
    schedule_enabled: bool = False
    schedule_time: str = "08:30"
    schedule_question: str = "生成今天的晨報"

    # ---------- 交付 ----------
    web_base_url: str = "http://127.0.0.1:8130"

    # ---------- Telegram 交付 ----------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = ""

    # ---------- 儲存 ----------
    ledger_path: str = "data/a5_ledger.db"
    brief_target_max_minutes: int = 10

    @field_validator("brain_topology")
    @classmethod
    def _validate_topology(cls, v: str) -> str:
        v = (v or "lens").strip().lower()
        if v not in {"rule", "shared", "lens", "split"}:
            raise ValueError("PQ_BRAIN_TOPOLOGY 只能是 rule / shared / lens / split")
        return v

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key) and self.brain_topology != "rule"

    @property
    def ledger_file(self) -> Path:
        p = Path(self.ledger_path)
        return p if p.is_absolute() else PROJECT_ROOT / p


settings = Settings()
