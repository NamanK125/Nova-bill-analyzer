from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM provider: "vllm" (self-hosted, OpenAI-compatible) or "openai" (api.openai.com)
    llm_provider: str = "vllm"

    # LLM transport (vLLM path)
    llm_base_url: str = "http://localhost:8000/v1"
    llm_api_key: str = "EMPTY"
    vision_base_url: str | None = None
    text_base_url: str | None = None

    vision_model: str = "Qwen/Qwen2.5-VL-7B-Instruct"
    text_model: str = "Qwen/Qwen2.5-7B-Instruct"
    small_text_model: str = "Qwen/Qwen2.5-7B-Instruct"

    # Qwen3 family ships reasoning ("thinking") mode on by default — vLLM emits
    # <think>...</think> blocks ahead of the JSON, breaking `response_format`.
    # The LLM client passes this through via `extra_body.chat_template_kwargs`
    # only when llm_provider == "vllm".
    qwen3_disable_thinking: bool = True

    # OpenAI provider (assessors can flip llm_provider=openai to bypass vLLM entirely)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"  # multimodal, supports response_format=json_object

    extractor_temperature: float = 0.0
    validator_temperature: float = 0.0
    router_temperature: float = 0.2
    nl_query_temperature: float = 0.0

    extractor_max_tokens: int = 2048
    validator_max_tokens: int = 1024
    router_max_tokens: int = 512

    # Storage
    db_url: str = "sqlite+aiosqlite:///./data/nova.db"
    db_url_sync: str = "sqlite:///./data/nova.db"
    checkpoint_db: str = "./data/checkpoints.db"

    # App
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    samples_dir: Path = Field(default_factory=lambda: Path("./samples"))
    artifacts_dir: Path = Field(default_factory=lambda: Path("./data/artifacts"))
    schemas_dir: Path = Field(default_factory=lambda: Path("./src/nova/schemas"))

    # Guardrails
    cost_budget_usd: float = 0.50
    stage_timeout_extractor_s: int = 60
    stage_timeout_validator_s: int = 20
    stage_timeout_router_s: int = 5
    max_retries_per_stage: int = 2

    # Confidence
    confidence_accept: float = 0.95
    confidence_floor: float = 0.70

    # Cost model (per 1k tokens — rough estimates for vLLM-hosted Qwen)
    # Used only for the ledger; real cost is GPU-time, surfaced as $/hr × duration.
    vision_cost_per_1k_in: float = 0.0008
    vision_cost_per_1k_out: float = 0.0016
    text_cost_per_1k_in: float = 0.0002
    text_cost_per_1k_out: float = 0.0004

    # OpenAI cost model — gpt-4o-mini: $0.15/M in, $0.60/M out (text + vision text-tokens)
    openai_cost_per_1k_in: float = 0.00015
    openai_cost_per_1k_out: float = 0.00060

    @property
    def vision_endpoint(self) -> str:
        if self.llm_provider == "openai":
            return self.openai_base_url
        return self.vision_base_url or self.llm_base_url

    @property
    def text_endpoint(self) -> str:
        if self.llm_provider == "openai":
            return self.openai_base_url
        return self.text_base_url or self.llm_base_url

    @property
    def effective_api_key(self) -> str:
        if self.llm_provider == "openai":
            return self.openai_api_key
        return self.llm_api_key

    @property
    def effective_vision_model(self) -> str:
        return self.openai_model if self.llm_provider == "openai" else self.vision_model

    @property
    def effective_text_model(self) -> str:
        return self.openai_model if self.llm_provider == "openai" else self.text_model

    @property
    def effective_small_text_model(self) -> str:
        return self.openai_model if self.llm_provider == "openai" else self.small_text_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
