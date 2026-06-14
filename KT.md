# Nova Trade-Doc — Knowledge Transfer (Agentic AI Architecture)

> Audience: agentic AI engineer presenting to a technical panel.
> Purpose: explain what was built, why every decision was made, and how the theory maps to the code.

---

## 0. One-Paragraph Summary

Nova Trade-Doc is a **five-stage LangGraph pipeline** that accepts a Bill of Lading PDF, runs it through three cooperating LLM agents (Extractor → Validator → Router), persists every intermediate artifact to a queryable work-store, and streams the full decision chain to a single-page UI. The system runs end-to-end in ~6 seconds at $0.005 per shipment — 100× under the $0.50 cost budget — with a hard architectural guarantee that no bad declaration can auto-approve without a human seeing it.

---

## 1. The Problem That Justifies The Architecture

Every international shipment produces 4–10 documents. A trade-ops analyst reads each one, cross-checks fields against a customer-specific ruleset, and decides: auto-approve, request an amendment, or escalate. The worst failure is a wrong approval that reaches customs — four to five figures per incident.

Ten named failure modes exist in the current manual process. The architecture is designed to eliminate them:

| Failure mode | How this system addresses it |
|---|---|
| Silent field omission on bad scans | Explicit `unreadable_fields` channel; never silent |
| Cross-document value drift | Three-valued validator; `uncertain` forces human review |
| Tacit rules that live in heads | YAML ruleset — versioned, diffable, FDE-owned |
| Email threads as workflow | Work-store with audit trail + state machine |
| Re-validation impossible after rule changes | Cached extractions → replay validator on new rules (pennies) |
| After-hours latency | Async pipeline runs without human presence |
| Junior/senior asymmetry | Router surfaces priority; seniors see only exceptions |
| No precision in supplier feedback | Structured `discrepancies[]` with field + found + expected |

---

## 2. Theoretical Foundation: What Makes This "Agentic"

A plain LLM pipeline is `prompt → model → output` — one-shot, stateless, opaque. An **agentic system** adds four properties:

1. **Autonomous decision-making** — the system chooses the next action from intermediate state, not from a pre-scripted flow
2. **Tool use / environment interaction** — agents act on external systems (PDF renderer, YAML rules engine, SQL store)
3. **Persistence of reasoning** — intermediate artifacts are durable, auditable, replayable; the *work* is the product, not just the final answer
4. **Human-in-the-loop escalation** — the agent knows when it doesn't know, and routes to a human rather than guessing

This system implements all four. More precisely, it is an instance of the **Perception → Judgment → Action** triad — the canonical breakdown of any agentic decision system:

```
Perception  →  Extractor   (what does the document say?)
Judgment    →  Validator   (is what it says correct against the rules?)
Action      →  Router      (what should the system do about it?)
```

This split is deliberate (ADR 002). It matches the shape of the work, not an arbitrary decomposition. Any coarser split (one agent) collapses audit trails. Any finer split (five agents) fragments concerns without separating them.

### Why Sequential Pipeline, Not Other Patterns

A recent multi-agent benchmark (arxiv 2603.22651) compared four orchestration patterns:
- Sequential pipeline
- Parallel fan-out with merge
- Hierarchical supervisor-worker
- Reflexive self-correcting loop

For trade-doc validation — where stages are **dependent** (validation depends on extraction; routing depends on validation) and the workflow shape is **fixed** — sequential pipeline wins. Reflexive self-correction appears *within* the Extractor (the re-extraction ladder on bad confidence) but is not the top-level pattern.

---

## 3. The Five-Stage LangGraph Pipeline

```
PDF → [FastAPI] → LangGraph StateGraph
                    │
                    ├── 1. scope_resolution        deterministic
                    │       which customer, doc type, rule-set version
                    │
                    ├── 2. context_compilation     deterministic
                    │       verify ruleset exists; future: load RAG context
                    │
                    ├── 3. schema_routing          deterministic
                    │       load typed field schema for this doc type
                    │
                    ├── 4. plan + execute          ← the three LLM agents
                    │       ├── A. Extractor       vision LLM → structured JSON
                    │       ├── B. Validator       deterministic rule engine
                    │       └── C. Router          decision matrix + LLM rationale
                    │
                    └── 5. evidence_delivery       deterministic
                            set final state; push events to UI over WebSocket
```

**Stages 1, 2, 3, 5 are deterministic.** No LLM calls. LLMs are used only where judgment is required. The orchestrator owns scope, state, and delivery; the agents own the hard thinking.

### Why LangGraph (ADR 001)

- It is Nova's actual production stack
- Native `AsyncSqliteSaver` checkpointing: if a worker crashes mid-pipeline, the graph resumes from the last successful node — no re-OCR
- Graph-as-code: the same Python definition runs as a laptop prototype and behind Nova's React Flow visual editor
- Each node is independently observable, re-runnable, and A/B-testable

**What is NOT done:** LLM calls are not wrapped in LangChain `Runnable` abstractions. Each agent is a single `async` function with a typed Pydantic input and a typed Pydantic output. The framework owns the DAG; the agents own the logic.

### Shared State (GraphState)

```python
# src/nova/orchestrator/state.py
class GraphState(TypedDict, total=False):
    shipment_id: str
    customer_id: str
    pdf_path: str
    doc_type: str
    rule_set_id: str
    doc_schema: dict          # loaded YAML — bound at schema_routing
    extraction: dict | None   # ExtractedDoc (serialised)
    extraction_id: str | None
    validation: dict | None   # ValidationReport (serialised)
    validation_id: str | None
    decision: dict | None     # RouterDecision (serialised)
    decision_id: str | None
    costs: list[dict]         # per-stage cost ledger
    errors: list[str]
    stage_events: list[dict]  # streamed to UI over WebSocket
```

Each node receives the full state, returns a **partial update** dict, and LangGraph merges it. Agents hand off via this shared state, not via direct function calls or shared memory.

---

## 4. Agent A: Extractor (Perception Layer)

**File:** `src/nova/agents/extractor.py`

**Job:** Convert PDF page images → 8 typed, source-grounded fields, each with `{value, confidence, quote, source}`.

### Why Vision-Language Model, Not OCR + Text

Modern VLMs do OCR and semantic understanding in one step. Splitting them loses cross-field calibration: the VLM disambiguates "consignee" partly from knowing it's reading a Bill of Lading and that "shipper" is already populated. A separate OCR pre-processor would be redundant and lose that signal.

### Closed Schema + Structured Output

The VLM is asked to fill a closed Pydantic schema (`_ExtractorPayload`). The model cannot invent new field names. `response_format=json_object` enforces JSON output; the Pydantic schema is also embedded in the system prompt as a fallback for endpoints that don't honour the header.

```python
class _ExtractorPayload(BaseModel):
    consignee_name: _FieldPayload
    hs_code: _FieldPayload
    # ... 6 more required fields
    unreadable_fields: list[str]  # first-class, not silent omission
```

### Quote-Verification Pass

After the VLM call, every extracted field's `quote` is verified against the PDF text layer using:
1. Exact substring match (normalised)
2. Windowed `difflib.SequenceMatcher` ratio ≥ 0.85

If neither passes: `confidence` is demoted to ≤ 0.55. The field is not discarded — it is surfaced with a lower confidence so the downstream validator can force `uncertain`.

### The Critical Empirical Finding: Model Miscalibration

> Qwen2.5-VL-7B given a BoL with the gross-weight digits covered by an opaque ink-blot returned `"1240.5 KG"` at **0.97 confidence**. It pattern-matched the field label and context to hallucinate a plausible number. Strengthening the system prompt reduced confidence by only ~0.02.

This is a **model-level miscalibration**, not a prompting problem. The architectural response (ADR 005):

```python
# src/nova/agents/extractor.py:211-219
if method == ExtractionMethod.vlm_only_unverified:
    cap = min(s.confidence_accept - 0.05, 0.90)  # 0.90 < accept threshold 0.95
    for fid, field in final_fields.items():
        if field.confidence > cap:
            final_fields[fid] = ExtractedField(
                value=field.value, confidence=cap, ...
            )
```

When there is no PDF text layer to cross-check against, the system **caps all field confidences at 0.90** — below the 0.95 accept threshold — as a system-level policy. Effect: 8/8 rules become `uncertain` → router escalates to `human_review`. The architectural guarantee holds regardless of which model is underneath.

**Contrast with Qwen3.6-35B:** the larger MoE model returned `" KG"` at **0.40 confidence** on the same input — it correctly recognised the digits were gone. The system policy cap is the safety net; a better model's self-awareness is the upside.

---

## 5. Agent B: Validator (Judgment Layer)

**File:** `src/nova/agents/validator.py`

**Job:** Evaluate every rule in the customer YAML ruleset against the extracted JSON. Return `match / mismatch / uncertain` per rule with `found / expected / reason`.

### Why Fully Deterministic (No LLM)

Rule evaluation is pure logic. An LLM is not needed and would add non-determinism at the highest-stakes stage. The validator dispatches on `rule.type`:

| Rule type | Example | Evaluator |
|---|---|---|
| `membership` | HS code in approved list | Set membership check |
| `regex` | Invoice number matches `INV-YYYY-NNN` | `re.match` |
| `presence` | Description of goods ≥ 8 chars | `len(value.strip())` |

### Three-Valued Output — The Critical Design Choice

Binary validators say "pass" or "fail". This validator says **match / mismatch / uncertain**. `uncertain` is the safe failure mode — it forces `human_review` at the router. Silent approval when in doubt is the failure this system is built to prevent.

The `uncertain` cascade logic:

```python
# src/nova/agents/validator.py:122-141
if field_id in extracted.unreadable_fields:
    return RuleResult(status=ValidationStatus.uncertain, ...)

if field.confidence < accept_threshold:  # default 0.95
    return RuleResult(
        status=ValidationStatus.uncertain,
        reason=f"Extracted confidence {field.confidence:.2f} is below accept threshold..."
    )
# Only then: dispatch to membership / regex / presence evaluator
```

Even if a value *happens* to match the rule, if the extractor wasn't confident about that value, the rule result is `uncertain`. This prevents the cascade: low-confidence extraction → silent approval.

### YAML Ruleset — FDE Customisation Layer

```yaml
# src/nova/schemas/customers/acme/rules.yaml
rule_set_id: acme@v1
rules:
  - id: hs_code_in_approved_list
    type: membership
    field: hs_code
    allowed: ["8471.41", "8517.62", "3004.90", "9018.90"]
    severity: high

  - id: gross_weight_positive_with_unit
    type: regex
    field: gross_weight
    pattern: "^[0-9]+(?:[.,][0-9]+)?\\s*(KG|MT|LBS|kg|mt|lbs)$"
    severity: medium
```

A new customer is onboardable by an FDE writing a YAML file. No code changes. This is the "generic engine + FDE customisation" architecture from Nova's positioning.

---

## 6. Agent C: Router (Action Layer)

**File:** `src/nova/agents/router.py`

**Job:** Map validation results → one of `{auto_approve, human_review, draft_amendment}` with a written rationale.

### Rules-First, LLM Second

The decision is **deterministic** — loaded from a customer YAML decision matrix, evaluated top-down (first match wins):

```yaml
# src/nova/schemas/customers/acme/decision.yaml
matrix:
  - when: {any_uncertain: true}
    decision: human_review
    reason: "At least one rule is uncertain — never silently approve uncertain results."

  - when: {any_mismatch_severity: high}
    decision: human_review
    reason: "High-severity mismatch (HS code, consignee) — human must confirm."

  - when: {any_mismatch_severity: medium}
    decision: draft_amendment
    reason: "Medium-severity mismatch — draft amendment for supplier."

  - when: {default: true}
    decision: auto_approve
    reason: "All rules match and confidences high — auto-approve."
```

The LLM is called **only to phrase the rationale** — the human-readable explanation — and to draft the amendment email body. The decision itself never depends on the LLM.

```python
# src/nova/agents/router.py:52-72
def _apply_matrix(report, *, cost_budget_exceeded, matrix):
    for row in matrix:
        when = row["when"]
        if when.get("any_uncertain") and report.has_any_uncertain:
            return Decision(row["decision"]), row["reason"]
        if when.get("any_mismatch_severity") in severities_of_mismatch():
            return Decision(row["decision"]), row["reason"]
    return Decision.auto_approve, "Default fallthrough."
```

**Why small LLM for rationale only:** decision space is 3 options. A 70B model is wasted compute. The rationale LLM (`Qwen2.5-7B`) receives the full structured context (decision, validation results, discrepancies) and writes one paragraph for the operator. If it fails, `deterministic_reason` is used as fallback — the decision stands either way.

---

## 7. The "Never Silently Approve" Guarantee — Five Layers

This is the load-bearing safety property of the entire architecture. It is implemented in five independent layers so no single failure can breach it:

| Layer | Location | What it catches |
|---|---|---|
| **1. Closed schema** | `extractor.py` Pydantic model | Model cannot invent fields |
| **2. Quote channel** | `extractor.py` quote-verification | Model must point at actual source text |
| **3. System-policy confidence cap** | `extractor.py:211-219` | Over-confident VLM on image-only scans |
| **4. Three-valued validator** | `validator.py:122-141` | Low-confidence field → `uncertain`, not `match` |
| **5. Router decision matrix** | `router.py` + `decision.yaml` | Any `uncertain` → `human_review` |

**The logic:** the only way to reach `auto_approve` is for every rule to return `match`, every field to be above 0.95 confidence, and the PDF to have a text layer that verifies every quote. All five layers must pass simultaneously. Each layer is independent — a failure at any layer escalates to `human_review` rather than passing through.

---

## 8. Agent Communication: Typed JSON Artifacts, Not Shared Memory

Agents do not call each other. They do not share in-process memory. Communication is:

1. Agent A writes its output to the work-store (`extractions` table)
2. The LangGraph orchestrator passes the serialised artifact ID in `GraphState`
3. Agent B reads the artifact by ID and runs against it
4. Agent B writes its output to the work-store (`validations` table)
5. Agent C reads both artifacts and writes its output (`decisions` table)

Properties this enables:
- **Restartability** — any stage can re-run from its inputs without re-running prior stages
- **Auditability** — every intermediate artifact is durable evidence, queryable months later
- **Independence** — each agent can be upgraded, A/B-tested, or swapped without touching others
- **Re-validation** — a rule change re-runs only the validator against cached extractions: pennies, not dollars

---

## 9. Agent D: NL→SQL (Operator Query Layer)

**File:** `src/nova/store/nl_query.py`

**Job:** Translate a plain-English question into a SQL SELECT and execute it against the work-store.

**Architecture:**
- Small text LLM (`Qwen2.5-7B`) with the full schema in the system prompt + 8 few-shot SQL examples
- Two safety gates before execution: regex for `SELECT` only; forbidden-keyword lint for `INSERT/UPDATE/DELETE/DROP/...`
- SQLAlchemy-level `PRAGMA query_only = 1` — read-only at the connection level
- Returns both the generated SQL and the result rows — the operator can verify the agent's interpretation

**Why this matters (System of Outcomes):** this is what makes the work-store valuable as an operational tool rather than an audit archive. An operator can ask `"how many shipments were flagged this week?"` and get an answer in <500ms without an engineer or a BI tool.

---

## 10. Storage: System-of-Outcomes Substrate (ADR 006)

The work-store schema is the concrete form of the "System of Outcomes" framing. Every *work unit* is a row:

```
shipments       → one row per document submitted
extractions     → one row per Extractor call (full JSON, cost, latency, model version)
validations     → one row per Validator call (results JSON, n_match, n_mismatch, n_uncertain)
decisions       → one row per Router call (decision, rationale, discrepancies JSON)
overrides       → one row per human override (operator_id, old decision, new decision, rationale)
```

**Why every intermediate artifact, not just the final decision:**
1. Re-validation under new rules is cheap (replay validator on cached extraction) vs. impossible (re-OCR the original PDF)
2. Compliance needs the per-field, per-rule evidence chain, not just "approved"
3. Human overrides become labeled examples only if both the system's output and the human's correction are durably stored — this is the feedback loop that makes agents improve over time

**Schema is ClickHouse-shaped even on SQLite:** append-only tables, denormalised JSON blobs for agent outputs, pre-aggregated counts (`n_match`, `n_mismatch`, `n_uncertain`) for cheap dashboard queries, `created_at` indexed on every table. Ports to ClickHouse without a schema change.

---

## 11. Model Layer: Swappable Substrate

**File:** `src/nova/llm.py`, `src/nova/config.py`

All LLM calls go through the OpenAI-compatible `chat.completions` API pointed at a configurable `base_url`. One `.env` change switches the entire pipeline between:

| Provider | Model | Notes |
|---|---|---|
| vLLM (self-hosted) | Qwen2.5-VL-7B-Instruct-AWQ | ~5 GB, reference baseline, exhibits calibration failure |
| vLLM (self-hosted) | Qwen3.6-35B-A3B-AWQ-4bit | ~17 GB, MoE (35B total / 3B active), correct on ink-blot |
| OpenAI (hosted) | gpt-4o-mini | No GPU required, assessor fallback |

**The architectural point:** the model is a swappable substrate. The properties that matter — quote-verification, three-valued validation, confidence cap on unverifiable extractions, human-review escalation — are all implemented at the orchestration layer, not in the model. Replacing the model moves cost, latency, and recall numbers; it does not move the zero-silent-approval guarantee.

**Why self-hosted vLLM is the primary target:** pharma and manufacturing customers will not let commercial BoL content — HS codes, declared values, consignees — leave their VPC. Self-hosted is a procurement requirement, not a preference.

**JSON enforcement:** `response_format={"type":"json_object"}` + Pydantic schema embedded in the system prompt. If parsing fails, the parse error is appended to the conversation and the model gets one retry with the error context. If the retry also fails, `tenacity` stops after `max_retries_per_stage` and the stage raises — which the orchestrator catches and routes to `human_review`.

---

## 12. Generic Engine + FDE Customisation

Nova's positioning is "zero hardcoded business logic." This is an architectural commitment, not a slogan. The boundary:

| Built once (generic engine) | Per customer (FDE YAML) |
|---|---|
| LangGraph DAG, 5 stages | Customer ID, rule-set version |
| Extractor prompt, VLM call | Field schema for this customer's BoL variant |
| Validator rule dispatcher | `rules.yaml` — list of rules with type, field, params, severity |
| Router decision-matrix executor | `decision.yaml` — priority-ordered conditions → decisions |
| Work-store schema, NL→SQL agent | Data residency, retention policy |
| Eval harness | Customer's golden document set |

A new customer is onboardable by an FDE writing two YAML files and a few sample docs. No Python code changes. This is the "days, not weeks" bar described in the PRD.

---

## 13. Measured Numbers

Against `Qwen3.6-35B-A3B-AWQ-4bit` on a single-GPU vLLM endpoint:

| Scenario | Decision | Cost | Latency | Notes |
|---|---|---|---|---|
| clean BoL | auto_approve | $0.0050 | 5.97 s | 8/8 match at confidence 0.98 |
| mismatch BoL | human_review | $0.0050 | 4.37 s | HS 8471.30 not in approved list |
| uncertain BoL | human_review | $0.0051 | 6.65 s | 8/8 uncertain (ink-blot + confidence cap) |

- **Cost budget:** $0.50/shipment → running ~100× under budget
- **Tests:** 14/14 passing (validator, router, NL→SQL safety, synth)
- **Checkpoints:** 27 checkpoint rows + 102 write rows across 3 LangGraph threads

---

## 14. What This Prototype Is NOT (Honest Scoping)

- No auth, no multi-tenancy, no real email send
- Bbox highlighting uses quote-text matching, not pixel-accurate VLM bboxes
- Eval harness runs on synthesised docs (production: 500-doc human-labelled golden set)
- Observability is `structlog` + per-call cost ledger; no LangSmith / Langfuse
- No cross-document reconciliation (BoL ↔ Invoice ↔ Packing List) — single-doc in v0

All deliberate scope cuts. The production path is described in PRD §12.

---

## 15. Production Path (What's Next)

In priority order:

1. **Cross-doc reconciliation** — extend the Validator's context to all docs in a shipment; introduce a Reconciler agent between Validator and Router if cross-rule complexity demands it. Highest-value next bet.
2. **Override → golden-set feedback loop** — every human override becomes a labelled example, piped into the eval set and into prompt-time few-shot context. This is the System-of-Outcomes loop made literal: the system improves from human corrections.
3. **Supplier-facing pre-flight validation** — Liang (supplier) drops a draft doc and gets validation feedback in <60s before goods ship. Same agents, different UI surface.
4. **Confidence calibration** — isotonic regression on a labelled golden set to turn self-reported VLM confidence into a calibrated probability. At v0 we use a directional signal + system-policy cap; calibration is the v1 upgrade.

---

## 16. Key Files by Topic

| Topic | File |
|---|---|
| LangGraph DAG wiring | `src/nova/orchestrator/graph.py` |
| Node implementations (5 stages) | `src/nova/orchestrator/nodes.py` |
| Shared state definition | `src/nova/orchestrator/state.py` |
| Extractor agent | `src/nova/agents/extractor.py` |
| Validator agent | `src/nova/agents/validator.py` |
| Router agent | `src/nova/agents/router.py` |
| LLM client (OpenAI-compatible) | `src/nova/llm.py` |
| Pydantic contracts (all agent I/O) | `src/nova/types.py` |
| Work-store repository | `src/nova/store/repo.py` |
| NL→SQL agent | `src/nova/store/nl_query.py` |
| ACME rule set (FDE YAML) | `src/nova/schemas/customers/acme/rules.yaml` |
| ACME decision matrix (FDE YAML) | `src/nova/schemas/customers/acme/decision.yaml` |
| BoL field schema (generic engine) | `src/nova/schemas/doc_types/bill_of_lading.yaml` |
| FastAPI surface + WebSocket | `src/nova/api/main.py` |
| Single-page UI | `src/nova/api/static/index.html` |
| ADR 001 — LangGraph | `docs/adr/001-langgraph-for-orchestration.md` |
| ADR 002 — 3 agents not 1 | `docs/adr/002-three-agents-not-one.md` |
| ADR 003 — OpenAI transport | `docs/adr/003-openai-compatible-transport.md` |
| ADR 004 — SQLite / ClickHouse | `docs/adr/004-sqlite-now-clickhouse-later.md` |
| ADR 005 — Confidence handling | `docs/adr/005-confidence-and-uncertainty-handling.md` |
| ADR 006 — Artifacts persistence | `docs/adr/006-system-of-outcomes-substrate.md` |
| Model comparison | `docs/MODEL_COMPARISON.md` |
| End-to-end test report | `E2E_TEST_REPORT.md` |
| NL→SQL sample queries | `SAMPLE_QUERIES.md` |

---

## 17. Presentation Talking Points (90 seconds each)

### "What did you build?"
A three-agent pipeline — Extractor, Validator, Router — wrapped in a five-stage LangGraph DAG, with a queryable SQLite work-store and a streaming single-page UI. It processes Bills of Lading end-to-end: PDF in, structured decision out, with every intermediate artifact persisted and queryable in plain English.

### "Why three agents and not one?"
Three is the boundary that aligns with the shape of the work. One prompt collapses three kinds of work — vision OCR, symbolic rule reasoning, and operational decision-making — into one opaque call with no audit trail and no targeted retry. Five agents loses cross-field calibration in the extractor and cross-rule reasoning in the validator. Three agents = perception, judgment, action.

### "What's the hardest safety problem?"
Silent approval. A wrong auto-approve on an HS code reaches customs and costs $10k-$50k. The architecture has five independent layers to make that failure unreachable — and the most important one was discovered empirically: small VLMs are over-confident on obscured text. The system-policy confidence cap (capping all fields at 0.90 when there is no text layer to cross-check) is the architectural guard that holds regardless of which model is underneath.

### "How does this connect to Nova's broader platform?"
The three agents are stage 4 of Nova's five-stage LangGraph pipeline. Stages 1-3 and 5 are deterministic orchestrator code that handle scope, context, schema, and evidence delivery. The generic engine is the engine; the FDE YAML files are the customer configuration. A new customer is onboardable in days without a code change — that's the "zero hardcoded business logic" promise made concrete.

### "What's the north-star metric?"
Straight-Through Processing rate with zero downstream defect: the share of shipments that go from doc ingest to action with no human touch and no defect in a 14-day audit window. One number. The "zero defect" tail makes it testable — without it the metric is gameable by approving everything.
