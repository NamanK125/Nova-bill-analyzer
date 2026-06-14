"""C · Router agent.

Rules-first decision matrix loaded from the customer's `decision.yaml`. The LLM
is consulted only to *explain* the decision in human language and to draft the
amendment-request body. The decision itself is deterministic so it's auditable
and replayable.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, Field

from nova.config import get_settings
from nova.llm import LLM
from nova.types import (
    Decision,
    Discrepancy,
    ExtractedDoc,
    RouterDecision,
    Severity,
    StageCost,
    SuggestedAction,
    ValidationReport,
    ValidationStatus,
)

log = structlog.get_logger()


# Pydantic schema the rationale-LLM is asked to fill.
class _RationalePayload(BaseModel):
    rationale: str = Field(description="One paragraph for the operator, written like a senior ops analyst.")
    amendment_draft: str | None = Field(
        default=None,
        description="If decision is draft_amendment, a one-paragraph email body to the supplier listing the discrepancies. Otherwise null.",
    )


def _load_decision_matrix(rule_set_id: str) -> dict[str, Any]:
    s = get_settings()
    customer = rule_set_id.split("@")[0]
    path = s.schemas_dir / "customers" / customer / "decision.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def _apply_matrix(report: ValidationReport, *, cost_budget_exceeded: bool, matrix: list[dict[str, Any]]) -> tuple[Decision, str]:
    min_conf = min((r.confidence for r in report.results), default=1.0)

    def severities_of_mismatch() -> set[str]:
        return {r.severity for r in report.results if r.status == ValidationStatus.mismatch.value}

    for row in matrix:
        when = row["when"]
        if when.get("default"):
            return Decision(row["decision"]), row["reason"]
        if when.get("cost_budget_exceeded") and cost_budget_exceeded:
            return Decision(row["decision"]), row["reason"]
        if when.get("any_uncertain") and report.has_any_uncertain:
            return Decision(row["decision"]), row["reason"]
        sev = when.get("any_mismatch_severity")
        if sev and sev in severities_of_mismatch():
            return Decision(row["decision"]), row["reason"]
        floor = when.get("min_confidence_below")
        if floor is not None and min_conf < floor:
            return Decision(row["decision"]), row["reason"]
    return Decision.auto_approve, "Default fallthrough — no rule fired."


async def route(
    extracted: ExtractedDoc,
    report: ValidationReport,
    *,
    cost_budget_exceeded: bool = False,
    customer_id: str = "acme",
) -> tuple[RouterDecision, list[StageCost]]:
    s = get_settings()
    started = datetime.utcnow()

    matrix_doc = _load_decision_matrix(report.rule_set_id)
    decision, deterministic_reason = _apply_matrix(
        report, cost_budget_exceeded=cost_budget_exceeded, matrix=matrix_doc["matrix"]
    )

    # Build a structured list of discrepancies for the UI + amendment body.
    discrepancies = [
        Discrepancy(
            field=r.field or "(rule)",
            found=r.found,
            expected=r.expected,
            severity=Severity(r.severity) if isinstance(r.severity, str) else r.severity,
        )
        for r in report.results
        if r.status == ValidationStatus.mismatch.value
    ]

    # LLM is consulted only to phrase the rationale + (optional) amendment draft.
    llm = LLM()
    sys_prompt = (
        "You are a senior trade-ops analyst writing for another senior. "
        "Be terse and concrete. Quote the rule id and the conflicting value when relevant. "
        "Never invent details that aren't in the input."
    )
    user_prompt = json.dumps(
        {
            "decision": decision.value,
            "deterministic_reason": deterministic_reason,
            "customer_id": customer_id,
            "doc_id": extracted.doc_id,
            "rule_set_id": report.rule_set_id,
            "validation_results": [r.model_dump(mode="json") for r in report.results],
            "extracted_fields": {k: v.model_dump(mode="json") for k, v in extracted.all_fields().items()},
            "discrepancies": [d.model_dump(mode="json") for d in discrepancies],
            "ask": (
                "Write a 2-3 sentence rationale the operator will read. If decision is "
                "'draft_amendment', also draft a short, polite amendment-request email body "
                "to the supplier enumerating each discrepancy as a bullet."
            ),
        },
        ensure_ascii=False,
    )

    try:
        payload, llm_cost = await llm.text_json(
            system=sys_prompt,
            user=user_prompt,
            schema=_RationalePayload,
            model=s.small_text_model,
            temperature=s.router_temperature,
            max_tokens=s.router_max_tokens,
            stage="router",
        )
        rationale = payload.rationale or deterministic_reason
        amendment = payload.amendment_draft if decision == Decision.draft_amendment else None
        costs = [llm_cost]
    except Exception as e:  # LLM rationale is best-effort; the decision stands either way
        log.warning("router.rationale_llm_failed", err=str(e))
        rationale = deterministic_reason
        amendment = None
        ended = datetime.utcnow()
        costs = [
            StageCost(
                stage="router", model="fallback", tokens_in=0, tokens_out=0,
                cost_usd=0.0, latency_ms=int((ended - started).total_seconds() * 1000),
                started_at=started, ended_at=ended,
            )
        ]

    suggested = _suggest_action(decision, discrepancies, amendment_draft=amendment)

    return RouterDecision(
        doc_id=extracted.doc_id,
        decision=decision,
        rationale=rationale,
        discrepancies=discrepancies,
        suggested_action=suggested,
        router_version=f"matrix@{matrix_doc.get('rule_set_id','?')} + {s.small_text_model}@2026-05",
    ), costs


def _suggest_action(decision: Decision, discrepancies: list[Discrepancy], *, amendment_draft: str | None) -> SuggestedAction:
    if decision == Decision.auto_approve:
        return SuggestedAction(type="auto_complete", priority="low")
    if decision == Decision.draft_amendment:
        return SuggestedAction(
            type="send_amendment_draft",
            priority="normal",
            preload_fields=[d.field for d in discrepancies],
            amendment_draft=amendment_draft,
        )
    # human_review
    priority = "high" if any(d.severity == "high" or d.severity == Severity.high for d in discrepancies) else "normal"
    return SuggestedAction(
        type="queue_for_ops",
        priority=priority,
        preload_fields=[d.field for d in discrepancies] or ["all"],
    )
