# ADR 002 — Three agents, not one prompt and not five

**Decision.** Exactly three LLM-driven agents on the pipeline boundary: Extractor → Validator → Router.

**Why not one prompt.** A single mega-prompt forces the largest, most expensive model on every step. Failures collapse into "the model was wrong somewhere" — no isolation, no targeted retry, no per-stage eval. The output is one opaque blob; trade-doc validation requires a defensible audit trail per field. Re-validation under new rules would re-OCR every historical document instead of replaying cached extractions.

**Why not five.** Per-field extractors lose cross-field calibration (the VLM disambiguates the consignee partly from knowing it's reading a BoL and that shipper is populated). Per-rule-type validators lose cross-rule reasoning (HS-code checks can depend on country of origin). A separate OCR pre-processor is redundant — modern VLMs do OCR + understanding in one step. A "ruleset-compiler" agent (NL → YAML) is a *build-time* tool, not a runtime stage.

**Why three.** The three correspond to the three kinds of work being done: **perception (Extractor) → judgment (Validator) → action (Router)**. Any finer split fragments work without separating concerns.
