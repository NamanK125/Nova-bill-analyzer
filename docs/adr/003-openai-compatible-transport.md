# ADR 003 — OpenAI-compatible HTTP for LLM transport

**Decision.** All LLM calls go through the `openai` SDK pointed at a configurable `base_url`.

**Why.** The same code runs against vLLM (production target, fully OpenAI-compatible), Ollama (laptop dev), and any hosted endpoint. Swapping providers is one env var. No code change. No abstraction layer to maintain.

**JSON enforcement.** We pass `response_format={"type":"json_object"}` (supported by vLLM). When an endpoint doesn't honour it, the call still works because we (a) embed the Pydantic JSON schema in the system prompt and (b) retry once with the parse error appended to the conversation on validation failure (see `nova/llm.py::_chat_json`).

**Cost.** Token usage comes back in the standard `usage` field; we multiply by per-1k-token rates from `config.py` to maintain a per-shipment ledger. Real cost on vLLM is GPU-time × duration — the per-1k rate is a stand-in suitable for relative comparisons.
