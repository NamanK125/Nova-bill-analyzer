# Technical write-up — Nova trade-doc prototype

A take-home prototype for GoComet's Nova Full-Stack AI Engineer role.
Three agents over LangGraph, OpenAI-compatible LLM transport, single-page HTML UI.
**Runs end-to-end on a laptop in under five minutes.** All numbers in this document are
measured, not projected, unless explicitly tagged.

> Read this together with [`PRD.md`](./PRD.md) (product framing) and the ADRs in
> [`docs/adr/`](./docs/adr/) (one short rationale per defensible choice).

---

## 1 · Problem & scope

A trade-document operator at ACME Pharma receives Bills of Lading from carriers, must
verify eight required fields against their internal rules (approved consignees, HS codes,
ports, incoterms, gross-weight format, invoice format), and either auto-approves the
declaration, escalates to human review, or drafts an amendment for the carrier.

The prototype demonstrates the five behaviours (A–E) the brief requires on real input,
end-to-end, with a single click in the UI per scenario.

## 2 · Architecture in one paragraph

A FastAPI HTTP/WebSocket surface accepts a PDF and writes it to disk. A LangGraph
StateGraph then walks five stages — scope resolution, context compilation, schema routing,
plan+execute, evidence delivery — that mirror Nova's runtime. Stage 4 is where the three
agents live: an **Extractor** (multimodal VLM call, 8 typed fields with per-field confidence
and supporting quote), a **Validator** (deterministic YAML rule engine, three-valued status:
match / mismatch / uncertain), and a **Router** (decision matrix that maps the validation
table to one of {auto_approve, human_review, draft_amendment} plus an LLM-written rationale).
Every artefact persists to a SQLite work-store; an NL→SQL agent answers operator questions
over that store. The UI streams every stage event over WebSocket so the operator watches the
work happen.

```
PDF → [FastAPI] → LangGraph
                    ├── scope resolution        (deterministic)
                    ├── context compilation     (deterministic)
                    ├── schema routing          (deterministic)
                    ├── plan + execute
                    │     ├── A · Extractor    (VLM call)
                    │     ├── B · Validator    (YAML rules; three-valued)
                    │     └── C · Router       (matrix + rationale)
                    └── evidence delivery       (work-store + WS push)
                          ↓
                  SQLite ←─→ D · NL→SQL agent
                          ↑
                          └── E · UI (single HTML page, WS-streamed)
```

## 3 · Why the model is swappable

The LLM is reached over the OpenAI-compatible `chat.completions` API with
`response_format=json_object` and the `image_url` content channel — both natively supported
by vLLM (self-hosted) and OpenAI (hosted). Flipping `LLM_PROVIDER` in `.env` switches the
entire pipeline between:

- `Qwen/Qwen2.5-VL-7B-Instruct-AWQ` (dense, AWQ-INT4, ~5 GB weights)
- `cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit` (35B-MoE / 3B-active, AWQ-INT4, ~17 GB weights)
- `gpt-4o-mini` (OpenAI hosted, assessor-friendly)

No code changes. See [`docs/MODEL_COMPARISON.md`](./docs/MODEL_COMPARISON.md) for the
side-by-side and the **calibration result** below.

## 4 · The five behaviours, with file pointers

| | Behaviour                          | Implementation                                  |
|---|------------------------------------|-------------------------------------------------|
| A | Multimodal extraction with quotes  | `src/nova/agents/extractor.py`                  |
| B | Three-valued deterministic validation | `src/nova/agents/validator.py` + `src/nova/schemas/customers/acme/rules.yaml` |
| C | Decision routing + rationale       | `src/nova/agents/router.py` + `decision.yaml`   |
| D | NL→SQL over the work-store         | `src/nova/store/nl_query.py`                    |
| E | Streaming UI (single HTML page)    | `src/nova/api/static/index.html` + `src/nova/api/main.py` |

## 5 · The "never silently approve" property

The expensive failure in trade documents is a wrong `match` that lets a bad declaration
through to customs. Multiple layers exist explicitly to make that failure unreachable:

1. **Closed Pydantic schema.** The model cannot invent new field names — schema validation
   rejects them.
2. **Quote channel.** Every extracted field must come with a quote. The Extractor verifies
   each quote against the PDF text layer using exact substring OR a tolerant
   `difflib.SequenceMatcher` ratio ≥ 0.85. Missing/weak matches demote confidence to ≤ 0.55.
3. **System-policy confidence cap on unverifiable extractions.** When there is no text layer
   to verify against (image-only scan), every field's confidence is capped at
   `confidence_accept - 0.05 = 0.90`, below the 0.95 accept threshold. Effect: 8/8 rules
   become `uncertain` and route to `human_review`.
4. **Three-valued validator.** Any rule whose underlying field is below the 0.95 accept
   threshold is forced to `uncertain` regardless of whether the value matches the rule.
5. **Router decision matrix.** Any `uncertain` rule → `human_review`. Any high-severity
   `mismatch` → `human_review`. `auto_approve` requires every rule to come back `match`.

See [ADR 005](./docs/adr/005-confidence-and-uncertainty-handling.md) for the policy
discussion and the model-miscalibration finding that motivated layer (3).

## 6 · An empirical finding — model calibration changes the picture

The synthetic `uncertain` BoL is generated with an opaque rectangle stamped over the
gross-weight digits, then flattened to image-only PDF so there is no text layer to leak
ground truth.

- **Qwen2.5-VL-7B-Instruct-AWQ** returned `'1240.5 KG'` at **0.97 confidence** — it
  pattern-matched the field label and surrounding context to invent a plausible number.
  Even strengthening the system prompt with "do not guess at obscured characters" reduced
  the reported confidence by only ~0.02. This is a model-level miscalibration, not a
  prompting problem.
- **Qwen3.6-35B-A3B-AWQ-4bit** returned `' KG'` at **0.40 confidence** on the same input —
  it correctly recognised that the digits were gone.

The system-policy cap (layer 3 above) was added in response to the 2.5-VL-7B result so
that the architectural guarantee holds *regardless* of which model is underneath. The
3.6-35B result is an upside — fewer false escalations — not a precondition.

## 7 · Measured end-to-end numbers

Against the production-target model (`cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit`) on a single-GPU
vLLM endpoint:

| Variant   | Decision      | Cost    | Latency | Notes                                            |
|-----------|---------------|---------|---------|--------------------------------------------------|
| clean     | auto_approve  | $0.0050 | 5.97 s  | 8/8 match at 0.98                                |
| mismatch  | human_review  | $0.0050 | 4.37 s  | HS 8471.30 flagged, high-severity discrepancy    |
| uncertain | human_review  | $0.0051 | 6.65 s  | 8/8 uncertain (model conf 0.40 + system cap 0.90)|

Cost budget per shipment is $0.50 → we run ~**100× under budget**.
Tests: **14/14 passing** (validator, router, NL→SQL safety, synth).

## 8 · Stack and ADRs

- **Orchestration:** LangGraph StateGraph with `AsyncSqliteSaver` checkpointer — see
  [ADR 001](./docs/adr/001-langgraph-for-orchestration.md). 27 checkpoint rows + 102 write
  rows persisted across the 3-shipment run.
- **3 agents, not 1:** [ADR 002](./docs/adr/002-three-agents-not-one.md).
- **OpenAI-compatible transport:** [ADR 003](./docs/adr/003-openai-compatible-transport.md).
- **SQLite now, ClickHouse later:** [ADR 004](./docs/adr/004-sqlite-now-clickhouse-later.md).
- **Confidence + three-valued status:** [ADR 005](./docs/adr/005-confidence-and-uncertainty-handling.md).
- **System-of-Outcomes substrate:** [ADR 006](./docs/adr/006-system-of-outcomes-substrate.md).

## 9 · Operator surface (NL→SQL)

Four real example queries with the generated SQL and the rows returned are in
[`SAMPLE_QUERIES.md`](./SAMPLE_QUERIES.md). Safety properties: `PRAGMA query_only=1` at the
connection level + DDL/DML keyword lint before execution. 3/4 queries returned correct
answers in <500 ms; the 4th was a *recall* failure (wrong JSON path) not a *safety*
failure, and is fixed by adding one few-shot example.

## 10 · What this prototype is NOT

- No auth, no multi-tenancy, no real email send.
- Bbox highlighting uses quote-text matching, not pixel-accurate VLM bboxes.
- Eval harness runs on synthesised docs; production path would use a 500-doc human-labelled
  golden set with isotonic-regression confidence calibration.
- Observability is `structlog` + per-call cost ledger. No LangSmith/Langfuse.

These are deliberate scope cuts. PRD §12 lays out the production path.

## 11 · Where to look next

- Watch the demo: [`DEMO.md`](./DEMO.md) — 5-minute walkthrough mapping each click in the
  UI to the brief's A–E behaviours.
- Read the PRD: [`PRD.md`](./PRD.md) — full product framing in Nova/FDE/System-of-Outcomes
  terms.
- Run it: see `README.md` §"Laptop setup".
