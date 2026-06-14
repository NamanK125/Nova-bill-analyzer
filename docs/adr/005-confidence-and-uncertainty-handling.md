# ADR 005 — Confidence handling: never silently approve

**Decision.** Three-band confidence + a three-valued validator (`match` / `mismatch` / `uncertain`). The Validator demotes any rule to `uncertain` when the underlying extracted field's confidence is below the *accept* threshold (default 0.95). The Router's decision matrix routes any `uncertain` to `human_review`.

**Why.** Binary validators silently approve when they should hedge. In trade-doc work, a wrong "match" is the most expensive failure in the system — it lets bad declarations through to customs. We pay for the third value in extra ops queue traffic; the unit economics tolerate this many times over.

**Layered anti-hallucination.** The Extractor must produce a `quote` for every field. The quote is checked against the PDF text layer (`pdfplumber`) with a tolerant matcher (exact substring OR `difflib.SequenceMatcher` ratio ≥ 0.85 over windowed text). If absent, confidence is demoted to ≤ 0.55. The Pydantic schema is closed — the model cannot invent new field names. Fields explicitly marked `unreadable` are first-class output, not silent omissions.

## When there is no ground truth — system-policy cap

A real-world finding from the end-to-end test: small open VLMs (here Qwen2.5-VL-7B-AWQ) are **over-confident**. Given a Bill of Lading where the `gross_weight` digits were fully covered with an opaque black ink-smudge rectangle, the model still confidently returned the original value at 0.95–0.97 — it pattern-matched the field label and surrounding context to invent a plausible number. Strengthening the system prompt with explicit "do not guess at obscured characters" instructions reduced the reported confidence by ~0.02 but did not stop the hallucination.

This is a **model-level miscalibration**, not a prompt-engineering problem. The architectural response: when the Extractor has no way to cross-check the model's output against a ground truth (no PDF text layer, no OCR fallback yet), the system **caps the reported confidence below the accept threshold** (default `confidence_accept - 0.05 = 0.90`). Effect: a scanned BoL on the `vlm_only_unverified` path produces 8/8 `uncertain` rules → router escalates to `human_review` automatically.

This trades off recall (some scanned docs that would have been correctly auto-approved get escalated) for the property the PRD's north-star explicitly demands: **zero downstream defects on auto-approvals**. False approvals are the expensive failure; defensive routing to human review is the cheap one. Until we wire a calibration network or an OCR cross-check, the policy stands.

**What we are NOT doing.** No probabilistic calibration of self-reported confidences yet — at v0 we treat the model's number as a directional signal and let (a) the quote-verification be the hard guard when text-layer is present, and (b) the unverified-confidence-cap be the hard guard when it isn't. Calibration with isotonic regression on a labelled golden set is the v1 bet.
