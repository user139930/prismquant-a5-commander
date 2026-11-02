# -*- coding: utf-8 -*-
"""A5 設定：環境變數前綴 PQ_，讀取專案根目錄的 .env。"""
from pathlib import Path

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
    cors_origins: list[str] = []

    # 大模型
    llm_api_key: str = ""
    llm_model: str = "deepseek/deepseek-chat"
    llm_base_url: str = "https://openrouter.ai/api/v1"

    # 上游智能體
    a1_api_url: str = "http://127.0.0.1:8010"
    a2_api_url: str = "http://127.0.0.1:8120"


settings = Settings()
