# Nova trade-doc validation — prototype

Three agents (Extractor → Validator → Router) + a queryable work-store + a single-page UI,
built as the **plan + execute** core of GoComet Nova's 5-stage LangGraph pipeline.

LLMs are called over the OpenAI-compatible HTTP interface, so the same code runs against
self-hosted **vLLM** (the production target) or any compatible endpoint.

The product reasoning sits in [`./PRD.md`](./PRD.md). The architectural decisions sit in
[`docs/adr/`](./docs/adr/) — one short paragraph each.

---

## Laptop setup (5 minutes)

**Prereqs:** Python 3.11+, `poppler` (for `pdf2image`), an LLM endpoint.

```bash
brew install poppler                       # macOS — Linux: apt-get install poppler-utils
cp .env.example .env                       # then edit (see below)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
./run.sh
```

That script then:

1. Boots FastAPI on `http://localhost:8080/` and serves the UI.
2. Generates three sample BoLs (`clean`, `mismatch`, `uncertain`) into `samples/` if missing.
3. Waits for you to type **`done`** at the prompt.
4. Stops the server and runs the test suite, printing 14/14.

### Pointing it at an LLM

The same code runs against any OpenAI-compatible endpoint. Pick one in `.env`:

```bash
# Path A — self-hosted vLLM (what we tested with)
LLM_PROVIDER=vllm
LLM_BASE_URL=http://your-vllm-host:port/v1
VISION_MODEL=/home/naman/models/qwen36-27b-awq       # or Qwen/Qwen2.5-VL-7B-Instruct-AWQ
QWEN3_DISABLE_THINKING=true                          # Qwen3 family only

# Path B — OpenAI hosted (assessor-friendly, no GPU needed)
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

`curl http://localhost:8080/health` reports the active provider/model.

That's the whole demo loop.

> **Note on the UI.** It's a single static HTML page with vanilla JavaScript + Tailwind via
> CDN, served by FastAPI. No build step, no node_modules. I am not a React engineer; this
> shape was deliberate.

---

## Five behaviours (A–E) — where to look

| | Behaviour | File |
|---|---|---|
| **A** | Extractor — vision-LLM pulls 8 required fields with per-field confidence + source quote | `src/nova/agents/extractor.py` |
| **B** | Validator — match / mismatch / uncertain against a YAML ruleset; never silently approves | `src/nova/agents/validator.py` |
| **C** | Router — auto-approve / human-review / draft-amendment, with written rationale | `src/nova/agents/router.py` |
| **D** | Storage + NL→SQL query | `src/nova/store/` and `POST /query` |
| **E** | Minimal UI showing real state from a real run | `src/nova/api/static/index.html` |

---

## How it maps to Nova's 5-stage pipeline

```
scope resolution ─→ context compilation ─→ schema routing ─→ plan + execute ─→ evidence delivery
   (orchestrator)        (orchestrator)        (orchestrator)   (3 agents)        (work-store + UI)
```

Stages 1, 2, 3, 5 are deterministic orchestrator code. The three agents are stage 4.
See `src/nova/orchestrator/graph.py`.

---

## What the demo proves on real input

The seeded `mismatch` sample BoL puts HS code `8471.30` on the document, which is **not**
in ACME's approved list (`8471.41` / `8517.62` / `3004.90` / `9018.90`). When you click
that sample in the UI:

1. **A** — extracts all 8 fields with confidence + quote; quote-verification runs against
   the PDF text layer.
2. **B** — `hs_code_in_approved_list` returns `mismatch`, others return `match`.
3. **C** — the deterministic decision matrix routes to `human_review` (HS code is
   high-severity); the rationale-LLM explains why in human language.
4. **D** — type *"how many shipments were flagged this week?"* into the NL box and you'll
   get the generated SQL, the rows, and a one-line answer.
5. **E** — all of the above is visible side-by-side on one screen with the source PDF.

---

## Manual / advanced

- `make api` — start the server without the wrapper script.
- `make demo` — headless CLI run, prints A/B/C and the cost ledger to stdout.
- `make test` — pytest (deterministic; does not require vLLM).
- `make samples` — re-generate the synthetic BoLs.
- `make clean` — wipe `./data` and caches.

---

## Repo tour

```
src/nova/
  config.py              env-driven settings
  llm.py                 one OpenAI client; vision + text helpers; JSON-retry
  types.py               Pydantic contracts crossing agent boundaries
  orchestrator/          LangGraph 5-stage DAG, state, nodes
  agents/                extractor.py, validator.py, router.py
  store/                 SQLAlchemy models, repo, NL→SQL agent
  schemas/
    doc_types/           generic engine config — fields per doc type
    customers/acme/      FDE customisation — rules.yaml + decision.yaml
  api/                   FastAPI app
    static/index.html    the UI (single page, vanilla JS)
  pdf/                   pdf2image render + reportlab BoL synthesizer
  cli.py                 headless runner
tests/                   pytest — Validator + Router + synth checks (no LLM)
evals/                   3-variant eval harness skeleton
docs/adr/                one ADR per defensible tech choice
samples/                 generated BoL PDFs (after `make samples`)
```

---

## Honest scoping — what this prototype is NOT

- No auth, no multi-tenancy, no real email send.
- Bbox highlighting uses quote-text matching, not pixel-accurate VLM bboxes (acceptable
  for v0; see ADR 005 on why the quote channel is the deterministic guard regardless).
- The eval harness runs on synthesised docs; the production path uses a 500-doc
  human-labelled golden set.
- Tracing is `structlog` + a per-call cost ledger. No LangSmith / Langfuse.

These are deliberate cuts. See ADRs and PRD §12 ("What's Next") for the production path.

---

## Deliverables in this folder

| What | File |
|---|---|
| PRD | [`PRD.md`](./PRD.md) · [`PRD.pdf`](./PRD.pdf) |
| Technical write-up | [`TECHNICAL_WRITEUP.md`](./TECHNICAL_WRITEUP.md) · [`TECHNICAL_WRITEUP.pdf`](./TECHNICAL_WRITEUP.pdf) |
| Demo walkthrough | [`DEMO.md`](./DEMO.md) |
| Sample queries (NL→SQL) | [`SAMPLE_QUERIES.md`](./SAMPLE_QUERIES.md) |
| Model comparison | [`docs/MODEL_COMPARISON.md`](./docs/MODEL_COMPARISON.md) |
| ADRs | [`docs/adr/00*.md`](./docs/adr/) |
| End-to-end test report | [`E2E_TEST_REPORT.md`](./E2E_TEST_REPORT.md) |
| Sample documents | `samples/acme_bol_clean.pdf` (clean), `samples/acme_bol_uncertain.pdf` (messy, image-only, occluded weight), `samples/acme_bol_mismatch.pdf` |

Read order for the rubric: **PRD → TECHNICAL_WRITEUP → DEMO → ADRs in numeric order**.
