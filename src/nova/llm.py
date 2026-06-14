"""Thin wrapper over the OpenAI-compatible client.

One class, two endpoints (vision + text), retry-on-parse-failure for JSON outputs.
Anything fancier (LangChain abstractions, etc.) costs more than it saves here —
see ADR 005.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import structlog
from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from nova.config import get_settings
from nova.types import StageCost

log = structlog.get_logger()


class LLMParseError(ValueError):
    """LLM returned content that could not be parsed into the expected schema."""


class LLM:
    """One client, two transports.

    vLLM serves both vision and text models; in dev they may share an endpoint.
    """

    def __init__(self) -> None:
        s = get_settings()
        if s.llm_provider == "openai" and not s.openai_api_key:
            raise RuntimeError(
                "LLM_PROVIDER=openai but OPENAI_API_KEY is empty. "
                "Set OPENAI_API_KEY in .env or switch LLM_PROVIDER back to 'vllm'."
            )
        self._vision = AsyncOpenAI(base_url=s.vision_endpoint, api_key=s.effective_api_key)
        # OpenAI hosts one endpoint for both vision and text; vLLM may split.
        if s.llm_provider == "openai":
            self._text = self._vision
        else:
            self._text = AsyncOpenAI(base_url=s.text_endpoint, api_key=s.effective_api_key)
        self._s = s

    def _provider_extras(self) -> dict[str, Any]:
        """Per-provider kwargs passed through to chat.completions.create()."""
        if self._s.llm_provider == "vllm" and self._s.qwen3_disable_thinking:
            # vLLM passes `extra_body` through verbatim; Qwen3 chat template reads
            # `chat_template_kwargs.enable_thinking` to skip the <think>...</think>
            # preamble that would break response_format=json_object parsing.
            return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
        return {}

    # ── vision ────────────────────────────────────────────────────────────

    async def vision_json(
        self,
        *,
        system: str,
        user_text: str,
        image_paths: list[Path],
        schema: type[BaseModel],
        temperature: float | None = None,
        max_tokens: int | None = None,
        stage: str = "extractor",
    ) -> tuple[BaseModel, StageCost]:
        """Vision call expecting JSON conforming to a Pydantic schema."""
        temperature = temperature if temperature is not None else self._s.extractor_temperature
        max_tokens = max_tokens or self._s.extractor_max_tokens

        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for p in image_paths:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_url(p)},
                }
            )

        messages = [
            {"role": "system", "content": system + "\n\n" + _schema_instruction(schema)},
            {"role": "user", "content": content},
        ]

        return await self._chat_json(
            client=self._vision,
            model=self._s.effective_vision_model,
            messages=messages,
            schema=schema,
            temperature=temperature,
            max_tokens=max_tokens,
            stage=stage,
            kind="vision",
        )

    # ── text ──────────────────────────────────────────────────────────────

    async def text_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stage: str = "validator",
    ) -> tuple[BaseModel, StageCost]:
        """Text-only call expecting JSON conforming to a Pydantic schema."""
        temperature = temperature if temperature is not None else self._s.validator_temperature
        max_tokens = max_tokens or self._s.validator_max_tokens
        model = model or self._s.effective_text_model

        messages = [
            {"role": "system", "content": system + "\n\n" + _schema_instruction(schema)},
            {"role": "user", "content": user},
        ]

        return await self._chat_json(
            client=self._text,
            model=model,
            messages=messages,
            schema=schema,
            temperature=temperature,
            max_tokens=max_tokens,
            stage=stage,
            kind="text",
        )

    async def text(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 256,
        stage: str = "router",
    ) -> tuple[str, StageCost]:
        """Free-text completion (for rationale generation, NL responses, etc.)."""
        model = model or self._s.effective_small_text_model
        started = time.time()
        from datetime import datetime
        t0 = datetime.utcnow()
        resp = await self._text.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            **self._provider_extras(),
        )
        latency_ms = int((time.time() - started) * 1000)
        t1 = datetime.utcnow()
        content = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        in_tok = usage.prompt_tokens if usage else 0
        out_tok = usage.completion_tokens if usage else 0
        cost = self._cost("text", in_tok, out_tok)
        return content.strip(), StageCost(
            stage=stage, model=model, tokens_in=in_tok, tokens_out=out_tok,
            cost_usd=cost, latency_ms=latency_ms, started_at=t0, ended_at=t1,
        )

    # ── internals ─────────────────────────────────────────────────────────

    async def _chat_json(
        self,
        *,
        client: AsyncOpenAI,
        model: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        temperature: float,
        max_tokens: int,
        stage: str,
        kind: str,
    ) -> tuple[BaseModel, StageCost]:
        from datetime import datetime

        @retry(
            reraise=True,
            stop=stop_after_attempt(self._s.max_retries_per_stage + 1),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(LLMParseError),
        )
        async def _call() -> tuple[BaseModel, StageCost]:
            t0 = datetime.utcnow()
            started = time.time()
            # Both vLLM and OpenAI support response_format={"type":"json_object"};
            # safe-fail to plain prompt if a particular server build doesn't.
            extra: dict[str, Any] = {
                "response_format": {"type": "json_object"},
                **self._provider_extras(),
            }
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **extra,
                )
            except Exception as e:  # vLLM versions vary; retry without response_format
                log.warning("llm.response_format.unsupported", err=str(e), kind=kind)
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **self._provider_extras(),
                )
            latency_ms = int((time.time() - started) * 1000)
            t1 = datetime.utcnow()
            content = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            in_tok = usage.prompt_tokens if usage else 0
            out_tok = usage.completion_tokens if usage else 0
            cost = self._cost(kind, in_tok, out_tok)
            stage_cost = StageCost(
                stage=stage, model=model, tokens_in=in_tok, tokens_out=out_tok,
                cost_usd=cost, latency_ms=latency_ms, started_at=t0, ended_at=t1,
            )
            try:
                obj = schema.model_validate_json(_extract_json(content))
            except Exception as e:
                log.warning("llm.parse_error", err=str(e), preview=content[:300])
                # Append the parse error to the next prompt so the model can self-correct
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response could not be parsed against the required "
                            f"JSON schema. Parser error: {e}. Reply again with ONLY valid JSON "
                            "matching the schema. No prose."
                        ),
                    }
                )
                raise LLMParseError(str(e)) from e
            return obj, stage_cost

        return await _call()

    def _cost(self, kind: str, in_tok: int, out_tok: int) -> float:
        s = self._s
        if s.llm_provider == "openai":
            return (in_tok / 1000) * s.openai_cost_per_1k_in + (out_tok / 1000) * s.openai_cost_per_1k_out
        if kind == "vision":
            return (in_tok / 1000) * s.vision_cost_per_1k_in + (out_tok / 1000) * s.vision_cost_per_1k_out
        return (in_tok / 1000) * s.text_cost_per_1k_in + (out_tok / 1000) * s.text_cost_per_1k_out


# ─── helpers ───────────────────────────────────────────────────────────────


def _data_url(p: Path) -> str:
    """Base64-encode an image as a data URL for the OpenAI image_url channel."""
    suffix = p.suffix.lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png"}.get(suffix, "png")
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:image/{mime};base64,{b64}"


def _schema_instruction(schema: type[BaseModel]) -> str:
    """Embed the Pydantic JSON schema in the system prompt as fallback for non-vLLM endpoints."""
    js = schema.model_json_schema()
    return (
        "Respond ONLY with a single JSON object matching this schema. "
        "No prose, no markdown fence — just JSON.\n"
        f"Schema:\n```json\n{json.dumps(js, indent=2)}\n```"
    )


def _extract_json(s: str) -> str:
    """Tolerant JSON extractor — strips ```json fences if the model wrapped its output."""
    s = s.strip()
    if s.startswith("```"):
        # peel one ``` fence
        s = s.split("```", 2)[-1].strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    return s
