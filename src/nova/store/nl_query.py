"""D · NL→SQL agent.

Schema is in the system prompt; the model emits a single SELECT statement.
Read-only execution at the connection level. Returns both the SQL and the
result rows so the operator can verify the agent's interpretation.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

from nova.config import get_settings
from nova.llm import LLM
from nova.types import StageCost

log = structlog.get_logger()


SCHEMA_DESCRIPTION = """
Tables (SQLite):

shipments(shipment_id TEXT PK, customer_id TEXT, state TEXT, pdf_path TEXT,
          created_at DATETIME, updated_at DATETIME)
  -- state ∈ {ingested, scoped, extracting, extracted, validating, validated,
  --          routing, routed, completed, failed}

extractions(extraction_id TEXT PK, shipment_id TEXT FK, doc_type TEXT,
            extracted_json JSON, extractor_version TEXT, extraction_method TEXT,
            cost_usd REAL, latency_ms INT, tokens_in INT, tokens_out INT,
            created_at DATETIME)

validations(validation_id TEXT PK, shipment_id TEXT FK, extraction_id TEXT FK,
            rule_set_id TEXT, results_json JSON, validator_version TEXT,
            cost_usd REAL, latency_ms INT,
            n_match INT, n_mismatch INT, n_uncertain INT,
            created_at DATETIME)

decisions(decision_id TEXT PK, shipment_id TEXT FK, validation_id TEXT FK,
          decision TEXT, rationale TEXT, discrepancies_json JSON,
          router_version TEXT, cost_usd REAL, latency_ms INT, created_at DATETIME)
  -- decision ∈ {auto_approve, human_review, draft_amendment}

overrides(override_id TEXT PK, decision_id TEXT FK, operator_id TEXT,
          new_decision TEXT, rationale TEXT, created_at DATETIME)
"""

FEW_SHOT = """
Examples (study these before writing SQL):

Q: how many shipments were flagged this week?
SQL: SELECT COUNT(*) AS n FROM decisions d
     JOIN shipments s ON s.shipment_id = d.shipment_id
     WHERE d.decision IN ('human_review','draft_amendment')
       AND d.created_at >= date('now','-7 days');

Q: which rules failed most in the last 30 days?
SQL: SELECT json_extract(value, '$.rule_id') AS rule_id, COUNT(*) AS n
     FROM validations, json_each(json_extract(results_json, '$.results'))
     WHERE json_extract(value, '$.status') = 'mismatch'
       AND created_at >= date('now','-30 days')
     GROUP BY rule_id ORDER BY n DESC;

Q: total cost spent this month?
SQL: SELECT ROUND(SUM(cost_usd), 4) AS total_cost_usd FROM (
       SELECT cost_usd FROM extractions WHERE created_at >= date('now','start of month')
       UNION ALL SELECT cost_usd FROM validations WHERE created_at >= date('now','start of month')
       UNION ALL SELECT cost_usd FROM decisions   WHERE created_at >= date('now','start of month')
     );

Q: most recent 5 shipments and their decisions
SQL: SELECT s.shipment_id, s.customer_id, s.state, d.decision, d.created_at
     FROM shipments s LEFT JOIN decisions d ON d.shipment_id = s.shipment_id
     ORDER BY s.created_at DESC LIMIT 5;

Q: average extraction latency
SQL: SELECT ROUND(AVG(latency_ms),0) AS avg_latency_ms FROM extractions;

Q: how many shipments were auto-approved overnight?
SQL: SELECT COUNT(*) FROM decisions
     WHERE decision = 'auto_approve'
       AND time(created_at) BETWEEN '00:00' AND '08:00';

Q: which customers have the most mismatches?
SQL: SELECT s.customer_id, SUM(v.n_mismatch) AS mismatches
     FROM validations v JOIN shipments s ON s.shipment_id = v.shipment_id
     GROUP BY s.customer_id ORDER BY mismatches DESC;

Q: shipments still pending human review
SQL: SELECT s.shipment_id, s.created_at, d.rationale FROM decisions d
     JOIN shipments s ON s.shipment_id = d.shipment_id
     LEFT JOIN overrides o ON o.decision_id = d.decision_id
     WHERE d.decision = 'human_review' AND o.override_id IS NULL;
"""


class _SqlPayload(BaseModel):
    sql: str = Field(description="One SQLite SELECT statement, no trailing semicolon required.")
    explanation: str = Field(description="One sentence: what this query is computing.")


class NLQueryResult(BaseModel):
    question: str
    sql: str
    explanation: str
    columns: list[str]
    rows: list[list[Any]]
    answer: str
    cost_usd: float
    latency_ms: int


_SELECT_ONLY = re.compile(r"^\s*select\b", re.IGNORECASE | re.DOTALL)
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|create|alter|attach|pragma|replace)\b", re.IGNORECASE)


async def answer(question: str) -> NLQueryResult:
    s = get_settings()
    llm = LLM()
    sys = (
        "You are a SQL agent for the Nova work-store (SQLite). "
        "Translate the user's question into ONE read-only SELECT statement. "
        "You must not produce any non-SELECT SQL. Prefer concrete column "
        "names from the schema below. Use SQLite date functions.\n\n"
        f"{SCHEMA_DESCRIPTION}\n\n{FEW_SHOT}"
    )
    payload, cost = await llm.text_json(
        system=sys,
        user=f"Question: {question}\nReturn JSON with `sql` and `explanation`.",
        schema=_SqlPayload,
        model=s.small_text_model,
        temperature=s.nl_query_temperature,
        max_tokens=400,
        stage="nl_query",
    )
    sql = payload.sql.strip().rstrip(";")

    if not _SELECT_ONLY.match(sql):
        raise ValueError(f"NL→SQL produced non-SELECT query: {sql[:120]}")
    if _FORBIDDEN.search(sql):
        raise ValueError(f"NL→SQL produced forbidden keyword: {sql[:120]}")

    engine = create_engine(s.db_url_sync, echo=False, future=True)
    started = datetime.utcnow()
    try:
        with engine.connect() as conn:
            conn.execute(text("PRAGMA query_only = 1"))
            rs = conn.execute(text(sql))
            cols = list(rs.keys())
            rows = [list(r) for r in rs.fetchall()]
    except Exception as e:
        raise RuntimeError(f"SQL execution failed: {e}") from e
    ended = datetime.utcnow()
    elapsed_ms = int((ended - started).total_seconds() * 1000)

    # Build a one-line natural-language answer for the operator.
    if len(rows) == 1 and len(cols) == 1:
        ans = f"{cols[0]} = {rows[0][0]}"
    elif rows:
        ans = f"{len(rows)} row(s) returned."
    else:
        ans = "No rows."

    return NLQueryResult(
        question=question,
        sql=sql,
        explanation=payload.explanation,
        columns=cols,
        rows=rows,
        answer=ans,
        cost_usd=cost.cost_usd,
        latency_ms=cost.latency_ms + elapsed_ms,
    )
