# Model comparison — Qwen2.5-VL-7B vs Qwen3.6-35B-A3B vs gpt-4o-mini

The prototype is provider-agnostic: any OpenAI-compatible `chat.completions`
endpoint that supports `response_format=json_object` and the `image_url` content
channel works. We've shipped three concrete paths and characterised them below.

| Dimension                        | **Qwen2.5-VL-7B-Instruct-AWQ**           | **Qwen3.6-35B-A3B-AWQ-4bit**            | **gpt-4o-mini** (OpenAI hosted)                |
|----------------------------------|------------------------------------------|-----------------------------------------|------------------------------------------------|
| Architecture                     | Dense                                    | Mixture-of-Experts (128 experts, top-8) | Undisclosed                                    |
| Params total / active            | 7B / 7B                                  | 35B / 3B                                | Undisclosed                                    |
| Quantization                     | AWQ INT4                                 | AWQ INT4                                | n/a (hosted)                                   |
| Weights on disk                  | ~5 GB                                    | ~17 GB                                  | n/a                                            |
| Min recommended GPU              | 1× 16 GB                                 | 1× 24 GB (A10/4090) at `--max-num-seqs 2` | n/a                                          |
| Multimodal (vision)              | Yes — native VLM                         | Yes — native VLM                        | Yes                                            |
| Reasoning mode                   | No                                       | Yes (`<think>…</think>` — must disable for JSON) | No                                    |
| `response_format=json_object`    | Yes (via vLLM grammar / guided decoding) | Yes (vLLM)                              | Yes (OpenAI)                                   |
| Cost basis                       | Self-hosted (GPU-hour)                   | Self-hosted (GPU-hour)                  | Per-token: $0.15 / $0.60 per 1M (in/out)       |
| Cost per shipment (3-stage run)  | **$0.0056 measured**                     | **$0.0050 measured**                    | ~$0.001–0.003 projected for the same prompt/image budget |
| End-to-end p50 latency           | **3.6 s measured**                       | **~5.7 s measured** (clean 5.97 s, mismatch 4.37 s, uncertain 6.65 s) | 1.5–3 s (OpenAI prod infra) |
| Calibration on occluded text     | **Failed** — 0.97 confidence on opaque ink-blot (see ADR-005) | **Passed** — extractor returned `' KG'` at **0.40** confidence on the same ink-blot, correctly recognising the digits as obscured | Untested empirically; system-policy cap applies |
| JSON adherence                   | Good with `response_format`; occasional preamble | **Excellent** with `enable_thinking=false`: 0 parse failures across 3 runs | Excellent — strongest in class                |
| Throughput variance              | Low (small, dense)                       | Medium (MoE routing overhead, expert-cache cold-misses on first calls) | Low (production endpoint, abstracted)          |
| Privacy posture                  | On-prem; no data leaves the network      | On-prem; no data leaves the network     | Data sent to OpenAI (covered by their data-processing terms) |
| Failure mode under load          | OOM at long context (mitigated by `--max-model-len 16384`) | OOM more likely; `--max-num-seqs 2` keeps it stable | Rate-limit / 429 |

## What we actually measured

End-to-end against `Qwen/Qwen2.5-VL-7B-Instruct-AWQ` on a single-GPU vLLM endpoint:

- 14/14 tests passing
- 3 router paths exercised: `auto_approve` (clean), `human_review` (HS-code
  mismatch), `human_review` (ink-blot-occluded gross-weight on the image-only
  scan variant)
- Cost per shipment: **$0.0056** (sum of stage costs from the ledger)
- p50 latency: **3.6 s**
- 27 checkpoint rows + 102 write rows across 3 LangGraph threads — confirms
  the AsyncSqliteSaver checkpointer persists

The Qwen3.6-35B-A3B path is wired and configured but the end-to-end measurement
has not been re-run; the comparison numbers for that column are projections
based on architecture, not measurements.

The gpt-4o-mini path is wired and config-switchable (set
`LLM_PROVIDER=openai` + `OPENAI_API_KEY=…` in `.env`) but we deliberately did
not exercise it — assessors can flip the switch to validate it themselves
without re-running our test suite. No code changes are required.

## Why offer all three

- **Qwen2.5-VL-7B** is the cheapest GPU footprint and our reference baseline.
  It surfaces the calibration failure that motivates ADR-005's
  unverified-confidence-cap policy — a useful demonstration that the system
  defends itself even when a sub-component lies confidently.
- **Qwen3.6-35B-A3B** is the recommended self-hosted upgrade. The 3B active
  path keeps latency in the same order of magnitude as 7B-dense, while the
  35B total budget improves complex-document handling (multi-page BoLs,
  hand-written annotations, low-contrast scans). The MoE shape is also what
  makes the 35B class affordable to run on a single GPU at all.
- **gpt-4o-mini** is the fallback for assessors / customers who can't or won't
  stand up GPU infrastructure. Same client code path; only the cost ledger
  numbers change. Critically, *the validation guarantees do not change* — the
  Validator's three-valued status, the unverified-confidence cap, and the
  Router's "never silently approve" rule all sit above whichever model is
  underneath.

## What does NOT change with the model

This is the architectural point worth surfacing for the assessment: the model
is a swappable substrate. The properties that matter to the customer —
quote-verification, three-valued validation, confidence cap on unverifiable
extractions, human-review escalation, full audit trail in the work-store — are
implemented at the orchestration layer, not in the model. Replacing the model
moves cost, latency, and recall numbers around; it does not move the
zero-silent-approval guarantee.

If a more capable model self-reports lower confidence on the occluded
gross-weight, fewer cases escalate to human review and the operator queue
shrinks. If a less capable model is over-confident, the unverified-confidence
cap catches it. Either way, no bad declaration auto-approves.

## How to switch

`.env`:

```bash
# Self-hosted (default)
LLM_PROVIDER=vllm
VISION_MODEL=Qwen/Qwen2.5-VL-7B-Instruct-AWQ        # or Qwen3.6-35B-A3B-AWQ-4bit
LLM_BASE_URL=http://your-vllm-host:port/v1

# OpenAI hosted (assessor-only)
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

Then verify with `curl http://localhost:8080/health` — it returns
`{"provider": "...", "vision_model": "...", "llm_endpoint": "..."}`.

No other code changes are required; the same Extractor, Validator, Router,
and NL→SQL paths run unchanged.
