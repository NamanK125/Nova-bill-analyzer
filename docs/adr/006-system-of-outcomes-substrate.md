# ADR 006 — Persist every artifact (System-of-Outcomes substrate)

**Decision.** Every agent's output is persisted before the next stage runs. The work-store has five tables: `shipments`, `extractions`, `validations`, `decisions`, `overrides`. The first four are append-only; `overrides` is the human-feedback signal.

**Why.** Nova's positioning ("System of Outcomes", not SaaS) is concretely about capturing *work* — the reasoning, the intermediate state, the corrections — not just final records ([Primary VC: AI agents need systems of work, not record](https://www.primary.vc/articles/ai-agents-need-systems-of-work-not-record)). If we persist only the final decision, three architectural properties collapse:
  1. **Re-validation under new rules** — cheap (re-run validator on cached extraction) becomes impossible (re-OCR every doc).
  2. **Audit** — compliance needs the per-field, per-rule evidence chain, not just "approved".
  3. **Learning loop** — overrides only become labelled examples if both the system's output *and* the human's override are durably stored.

**Trade.** One extra write per stage. Storage cost on SQLite is negligible; on ClickHouse the schema is shaped for cheap append (see ADR 004).
