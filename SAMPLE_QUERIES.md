# Sample NL→SQL queries against the work-store

All four queries below were run against the live SQLite work-store after processing the three
sample BoLs (`clean`, `mismatch`, `uncertain`). The NL→SQL agent uses `cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit`
with 8 few-shot examples and `PRAGMA query_only=1` (read-only connection — DDL/DML are
hard-blocked regardless of what the model emits).

Source: `src/nova/store/nl_query.py`. Run via `POST /query` or via the UI bottom-right panel.

---

### 1 · How many shipments did we process, broken down by decision?

**SQL the agent generated:**
```sql
SELECT d.decision, COUNT(*) AS n_shipments
FROM decisions d
GROUP BY d.decision
ORDER BY n_shipments DESC
```

**Rows:**
| decision      | n_shipments |
|---------------|-------------|
| human_review  | 2           |
| auto_approve  | 1           |

---

### 2 · What was the average extractor cost per shipment in USD?

**SQL:**
```sql
SELECT ROUND(AVG(e.cost_usd), 4) AS avg_extractor_cost_usd FROM extractions e
```

**Rows:**
| avg_extractor_cost_usd |
|------------------------|
| 0.0047                 |

Comment: the per-shipment all-in (Extractor + Validator + Router) is **$0.0050** — see the
ledger lines in each CLI run. Cost budget per shipment is $0.50; we are ~100× under.

---

### 3 · Which HS codes have we seen across all processed shipments?

**SQL:**
```sql
SELECT DISTINCT json_extract(value, '$.hs_code') AS hs_code
FROM extractions, json_each(json_extract(extracted_json, '$.items'))
WHERE json_extract(value, '$.hs_code') IS NOT NULL
  AND json_extract(value, '$.hs_code') != ''
```

**Rows:** *(empty)*

Comment — **honest failure mode**: the model guessed at a `$.items[]` array path that does
not exist in our schema (HS code is `$.hs_code.value` directly). The query is *safe*
(read-only) and runs without error; it just returns zero rows. Two responses possible:
(a) add another few-shot example showing the correct JSON path; (b) consider a tools-style
schema-introspection step before generation. We took option (a) as the immediate fix and
left (b) on the v1 list.

---

### 4 · How many shipments needed human review?

**SQL:**
```sql
SELECT COUNT(*) AS n FROM decisions WHERE decision = 'human_review'
```

**Rows:**
| n |
|---|
| 2 |

---

## Safety properties demonstrated

- The connection is opened with `PRAGMA query_only = 1` — even if the model emitted a
  `DROP TABLE` or an `UPDATE`, SQLite would refuse the write at the storage layer.
- Generated SQL is also lint-checked for forbidden keywords (`DROP`, `DELETE`, `ALTER`, etc.)
  before execution as a belt-and-braces second guard.
- All four queries returned in <500 ms against the local SQLite file.
- Failure (query #3) was a *recall* failure — wrong JSON path — not a *safety* failure.
  The system stayed sound.
