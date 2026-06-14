"""B · Validator agent.

Evaluates a customer's YAML rule set against an ExtractedDoc. Returns
match/mismatch/uncertain per rule with found/expected/reason — never silently
approves.

Deterministic core dispatched on `rule.type`. An LLM is consulted only for
rules that explicitly ask for it (none in v0 ACME), keeping latency low and
behaviour predictable.

Uncertainty rules:
 - If the underlying extracted field's confidence < CONFIDENCE_ACCEPT, the
   rule result is downgraded to `uncertain` (even if values would otherwise
   match) — silent approval on shaky perception is the worst failure.
 - If the field is in `unreadable_fields`, the rule result is `uncertain`.
 - If the rule itself lacks required config (e.g. tolerance unset), `uncertain`.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
import yaml

from nova.config import get_settings
from nova.types import (
    ExtractedDoc,
    ExtractedField,
    RuleResult,
    Severity,
    StageCost,
    ValidationReport,
    ValidationStatus,
)

log = structlog.get_logger()


def _load_ruleset(rule_set_id: str) -> dict[str, Any]:
    s = get_settings()
    if "@" in rule_set_id:
        customer, version = rule_set_id.split("@", 1)
    else:
        customer, version = rule_set_id, "v1"
    path = s.schemas_dir / "customers" / customer / "rules.yaml"
    with open(path) as f:
        rules = yaml.safe_load(f)
    # Sanity-check version
    if rules.get("rule_set_id") != rule_set_id and rules.get("rule_set_id") != f"{customer}@{version}":
        log.warning("validator.ruleset_version_mismatch",
                    requested=rule_set_id, loaded=rules.get("rule_set_id"))
    return rules


async def validate(extracted: ExtractedDoc, *, rule_set_id: str = "acme@v1") -> tuple[ValidationReport, list[StageCost]]:
    """Run validation. Currently fully deterministic; cost is near-zero."""
    s = get_settings()
    started = datetime.utcnow()
    ruleset = _load_ruleset(rule_set_id)
    results: list[RuleResult] = []
    fields = extracted.all_fields()

    for rule in ruleset["rules"]:
        results.append(_eval_rule(rule, fields, extracted, accept_threshold=s.confidence_accept))

    ended = datetime.utcnow()
    cost = StageCost(
        stage="validator",
        model="deterministic",
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        latency_ms=int((ended - started).total_seconds() * 1000),
        started_at=started,
        ended_at=ended,
    )

    report = ValidationReport(
        doc_id=extracted.doc_id,
        rule_set_id=rule_set_id,
        results=results,
        validator_version=f"deterministic@2026-05",
    )
    log.info(
        "validator.done",
        doc_id=extracted.doc_id,
        n_match=sum(1 for r in results if r.status == ValidationStatus.match.value),
        n_mismatch=sum(1 for r in results if r.status == ValidationStatus.mismatch.value),
        n_uncertain=sum(1 for r in results if r.status == ValidationStatus.uncertain.value),
    )
    return report, [cost]


# ─── rule dispatcher ───────────────────────────────────────────────────────


def _eval_rule(
    rule: dict[str, Any],
    fields: dict[str, ExtractedField],
    extracted: ExtractedDoc,
    *,
    accept_threshold: float,
) -> RuleResult:
    rid = rule["id"]
    field_id = rule["field"]
    severity = Severity(rule.get("severity", "medium"))

    if field_id not in fields:
        return RuleResult(
            rule_id=rid, status=ValidationStatus.uncertain, confidence=0.5,
            field=field_id, reason=f"Rule references unknown field '{field_id}'.",
            severity=severity,
        )

    field = fields[field_id]

    # Confidence floor — if the extractor isn't sure, we cannot be sure either.
    # This is the single most important "never silently approve" guard.
    if field_id in extracted.unreadable_fields:
        return RuleResult(
            rule_id=rid, status=ValidationStatus.uncertain, confidence=field.confidence,
            field=field_id, found=str(field.value) if field.value is not None else None,
            expected=_expected_for(rule),
            reason=f"Field '{field_id}' was flagged unreadable by extractor (confidence={field.confidence:.2f}).",
            severity=severity,
        )

    if field.confidence < accept_threshold:
        return RuleResult(
            rule_id=rid, status=ValidationStatus.uncertain, confidence=field.confidence,
            field=field_id, found=str(field.value) if field.value is not None else None,
            expected=_expected_for(rule),
            reason=(
                f"Extracted confidence {field.confidence:.2f} is below accept threshold "
                f"{accept_threshold:.2f} — cannot decide match/mismatch safely."
            ),
            severity=severity,
        )

    # Dispatch
    rtype = rule["type"]
    if rtype == "membership":
        return _eval_membership(rule, field, severity)
    if rtype == "regex":
        return _eval_regex(rule, field, severity)
    if rtype == "presence":
        return _eval_presence(rule, field, severity)

    return RuleResult(
        rule_id=rid, status=ValidationStatus.uncertain, confidence=0.5,
        field=field_id, reason=f"Unknown rule type '{rtype}'.",
        severity=severity,
    )


# ─── individual rule evaluators ────────────────────────────────────────────


def _eval_membership(rule: dict[str, Any], field: ExtractedField, severity: Severity) -> RuleResult:
    rid = rule["id"]
    field_id = rule["field"]
    raw = str(field.value) if field.value is not None else ""
    candidate = raw
    if rule.get("extract_first_token"):
        candidate = raw.split()[0] if raw.split() else raw
    allowed: list[str] = rule.get("allowed", [])
    if rule.get("case_insensitive"):
        ok = candidate.lower() in {x.lower() for x in allowed}
    else:
        ok = candidate in allowed

    if ok:
        return RuleResult(
            rule_id=rid, status=ValidationStatus.match, confidence=field.confidence,
            field=field_id, found=raw, expected=f"one of {allowed}",
            reason="Value is on the approved list.", severity=severity,
        )
    return RuleResult(
        rule_id=rid, status=ValidationStatus.mismatch, confidence=field.confidence,
        field=field_id, found=raw, expected=f"one of {allowed}",
        reason=f"'{candidate}' is not on the approved list.",
        severity=severity,
    )


def _eval_regex(rule: dict[str, Any], field: ExtractedField, severity: Severity) -> RuleResult:
    rid = rule["id"]
    field_id = rule["field"]
    raw = str(field.value) if field.value is not None else ""
    pattern: str = rule["pattern"]
    try:
        ok = re.match(pattern, raw) is not None
    except re.error as e:
        return RuleResult(
            rule_id=rid, status=ValidationStatus.uncertain, confidence=field.confidence,
            field=field_id, found=raw, expected=f"matches /{pattern}/",
            reason=f"Rule pattern is invalid: {e}",
            severity=severity,
        )
    if ok:
        return RuleResult(
            rule_id=rid, status=ValidationStatus.match, confidence=field.confidence,
            field=field_id, found=raw, expected=f"matches /{pattern}/",
            reason="Value matches required format.", severity=severity,
        )
    return RuleResult(
        rule_id=rid, status=ValidationStatus.mismatch, confidence=field.confidence,
        field=field_id, found=raw, expected=f"matches /{pattern}/",
        reason=f"'{raw}' does not match the required format /{pattern}/.",
        severity=severity,
    )


def _eval_presence(rule: dict[str, Any], field: ExtractedField, severity: Severity) -> RuleResult:
    rid = rule["id"]
    field_id = rule["field"]
    raw = str(field.value) if field.value is not None else ""
    min_len = int(rule.get("min_length", 1))
    if len(raw.strip()) >= min_len:
        return RuleResult(
            rule_id=rid, status=ValidationStatus.match, confidence=field.confidence,
            field=field_id, found=raw[:80], expected=f"non-empty (≥ {min_len} chars)",
            reason="Field is present and non-empty.", severity=severity,
        )
    return RuleResult(
        rule_id=rid, status=ValidationStatus.mismatch, confidence=field.confidence,
        field=field_id, found=raw, expected=f"non-empty (≥ {min_len} chars)",
        reason=f"Field is too short ({len(raw.strip())} chars).",
        severity=severity,
    )


def _expected_for(rule: dict[str, Any]) -> str:
    rtype = rule["type"]
    if rtype == "membership":
        return f"one of {rule.get('allowed', [])}"
    if rtype == "regex":
        return f"matches /{rule.get('pattern','')}/"
    if rtype == "presence":
        return f"non-empty (≥ {rule.get('min_length',1)} chars)"
    return "n/a"
