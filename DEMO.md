# DEMO — Nova trade-doc prototype

A 5-minute walkthrough of the prototype. Open this side-by-side with the running app.

## TL;DR

A 3-agent pipeline (Extractor → Validator → Router) that turns a Bill of Lading PDF into an
auto-approve / human-review / draft-amendment decision, governed by per-customer YAML rules.
Architecture maps to Nova's 5-stage runtime (scope resolution → context compilation → schema
routing → plan+execute → evidence delivery). The same code runs against self-hosted vLLM
(Qwen2.5-VL-7B-AWQ, Qwen3.6-35B-A3B-AWQ-4bit) or OpenAI gpt-4o-mini — flip one env var.

## 1 · Boot

```bash
./run.sh                 # boots FastAPI + serves the UI on :8080
                         # waits at the prompt; type 'done' to run the test suite
```

Open http://localhost:8080. The landing page explains the architecture in three collapsible
bullets and offers three sample BoLs.

Verify the active backend:

```bash
curl -s localhost:8080/health
# {"provider":"vllm","vision_model":"cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit", ... }
```

## 2 · Behaviour walkthrough (A → E)

The brief asked for five behaviours. Each maps to one click in the UI:

### A · Multimodal extraction → Click **Clean BoL**
Watch the right panel: 8 fields appear with values, confidence bars, and quotes (hover a row
to see the supporting quote pulled from the PDF). Method shows `vlm_primary` —
multimodal VLM call with text-layer fuzzy-verification. **Expected:** every field at ~0.98.

### B · Deterministic validation → Same click, scroll to panel B
Eight rules from `src/nova/schemas/customers/acme/rules.yaml` evaluate against the extraction.
Three-valued status: match (green) / mismatch (red) / uncertain (amber). **Expected:** 8/8 match.

### C · Routing decision → Same click, scroll to panel C
Router applies the decision matrix from `customers/acme/decision.yaml`, generates an
operator-facing rationale via LLM, and surfaces an override button. **Expected:**
`AUTO_APPROVE`.

### Now click **Mismatch BoL**
The HS code `8471.30` is not in ACME's approved list — Validator returns `mismatch` on
`hs_code_in_approved_list` with severity=high. Decision matrix routes to `HUMAN_REVIEW`.
The rationale names the specific discrepancy.

### Now click **Uncertain BoL** — the interesting one
Gross-weight is covered by an opaque ink-blot rectangle. The PDF has been flattened to
image-only — there is no text layer to verify against.

Watch what happens:
- Extractor reads `' KG'` at confidence **0.40** for `gross_weight` — the VLM correctly
  recognises the digits are gone (this is Qwen3.6-35B-A3B — Qwen2.5-VL-7B previously
  hallucinated `'1240.5 KG'` at 0.97 on the same input; see `docs/MODEL_COMPARISON.md`).
- The remaining 7 fields are returned at 0.90 — capped by system policy because the
  document has no text layer to cross-check against (`vlm_only_unverified` path).
- Validator forces every rule to `uncertain` (any field below the 0.95 accept threshold can't
  be safely decided).
- Router routes to `HUMAN_REVIEW` — **8/8 uncertain**.

Two independent guards both fire: the model itself reports low confidence on the actually-
unreadable field, AND the system caps the unverifiable rest. This is the
"never silently approve" property the PRD's north-star requires.

### D · NL→SQL over the work-store → Bottom-right panel
Type any of:
- `how many shipments were flagged?`
- `average extractor cost in USD`
- `break down decisions by type`

It generates a safe SELECT (the connection has `PRAGMA query_only=1`), runs it, and shows
the rows + a one-sentence explanation. NL→SQL agent uses 8 few-shot examples; see
`src/nova/store/nl_query.py`.

### E · UI as the system-of-outcomes surface
Everything above runs over WebSocket — stages stream in as they complete. The override
button records human disagreement into the `overrides` table with operator id + rationale.
That's the labelled-data spine the next-version evaluator reads (`docs/adr/006-system-of-outcomes-substrate.md`).

## 3 · CLI mode (no browser)

```bash
python -m nova run samples/acme_bol_clean.pdf --customer acme
python -m nova run samples/acme_bol_mismatch.pdf --customer acme
python -m nova run samples/acme_bol_uncertain.pdf --customer acme
```

Each prints stage events, extraction, validation, decision, ledger.

## 4 · Tests

```bash
pytest -q                # 14/14 passing — extractor, validator, router, NL→SQL
```

## 5 · Switching the model

`/.env`:

```bash
# Self-hosted (default) — point at any OpenAI-compatible vLLM:
LLM_PROVIDER=vllm
LLM_BASE_URL=http://your-vllm:port/v1
VISION_MODEL=cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit       # or Qwen/Qwen2.5-VL-7B-Instruct-AWQ
TEXT_MODEL=cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit
SMALL_TEXT_MODEL=cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit
QWEN3_DISABLE_THINKING=true                          # Qwen3 family only

# OpenAI hosted (assessor-only path — we did not run this end-to-end):
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

No other changes. The full comparison is in `docs/MODEL_COMPARISON.md`.

## 6 · Measured numbers (Qwen3.6-35B-A3B-AWQ-4bit, single GPU)

| Variant   | Decision      | Cost   | Latency | Notes                                            |
|-----------|---------------|--------|---------|--------------------------------------------------|
| clean     | auto_approve  | $0.0050| 5.97 s  | 8/8 match at 0.98                                |
| mismatch  | human_review  | $0.0050| 4.37 s  | HS 8471.30 flagged, high-severity discrepancy    |
| uncertain | human_review  | $0.0051| 6.65 s  | 8/8 uncertain (model conf 0.40 + system cap 0.90)|

Cost budget per shipment: $0.50. Actual: ~$0.005 (100× under budget).

## 7 · What's where

- `PRD.md` — full product brief with Nova/FDE/System-of-Outcomes framing
- `docs/MODEL_COMPARISON.md` — Qwen2.5-VL-7B vs Qwen3.6-35B-A3B vs gpt-4o-mini side-by-side
- `docs/adr/00*.md` — 6 ADRs (LangGraph, 3-agent split, OpenAI-compatible transport, SQLite/ClickHouse, confidence handling, System-of-Outcomes)
- `E2E_TEST_REPORT.md` — first end-to-end test report (against Qwen2.5-VL-7B-AWQ)
- `src/nova/agents/` — extractor, validator, router
- `src/nova/orchestrator/graph.py` — LangGraph StateGraph + AsyncSqliteSaver checkpointer
- `src/nova/schemas/customers/acme/` — rules.yaml + decision.yaml (per-customer config)
- `src/nova/store/` — SQLite work-store + repo + NL→SQL agent
- `src/nova/api/main.py` — FastAPI + WebSocket
- `src/nova/api/static/index.html` — single HTML page (vanilla JS + Tailwind CDN)
- `tests/` — 14 tests covering all three agents + NL→SQL safety
