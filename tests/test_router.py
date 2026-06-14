"""Deterministic checks of the Router's decision matrix.

We stub the rationale-LLM call so the test runs without vLLM. The decision
itself is deterministic by design.
"""
from __future__ import annotations

import asyncio

import pytest

from nova.agents.router import _apply_matrix, _load_decision_matrix
from nova.types import (
    DocType,
    Decision,
    ExtractedDoc,
    ExtractedField,
    ExtractionMethod,
    RuleResult,
    Severity,
    ValidationReport,
    ValidationStatus,
)


def _ef(value: str, conf: float = 0.99) -> ExtractedField:
    return ExtractedField(value=value, confidence=conf, quote=value, source=None)


def _report(*results: RuleResult) -> ValidationReport:
    return ValidationReport(
        doc_id="t", rule_set_id="acme@v1",
        results=list(results), validator_version="test",
    )


@pytest.fixture(scope="module")
def matrix():
    return _load_decision_matrix("acme@v1")["matrix"]


def test_all_match_routes_to_auto_approve(matrix):
    rep = _report(
        RuleResult(rule_id="a", status=ValidationStatus.match, confidence=0.99, reason="ok", severity=Severity.high),
        RuleResult(rule_id="b", status=ValidationStatus.match, confidence=0.99, reason="ok", severity=Severity.low),
    )
    d, _ = _apply_matrix(rep, cost_budget_exceeded=False, matrix=matrix)
    assert d == Decision.auto_approve


def test_high_severity_mismatch_routes_to_human_review(matrix):
    rep = _report(
        RuleResult(rule_id="a", status=ValidationStatus.mismatch, confidence=0.99, reason="x", severity=Severity.high),
    )
    d, _ = _apply_matrix(rep, cost_budget_exceeded=False, matrix=matrix)
    assert d == Decision.human_review


def test_medium_mismatch_routes_to_draft_amendment(matrix):
    rep = _report(
        RuleResult(rule_id="a", status=ValidationStatus.mismatch, confidence=0.99, reason="x", severity=Severity.medium),
    )
    d, _ = _apply_matrix(rep, cost_budget_exceeded=False, matrix=matrix)
    assert d == Decision.draft_amendment


def test_uncertain_always_routes_to_human_review(matrix):
    """Even if the rest are match, uncertain → human review. Anti-silent-approval."""
    rep = _report(
        RuleResult(rule_id="a", status=ValidationStatus.match, confidence=0.99, reason="ok", severity=Severity.high),
        RuleResult(rule_id="b", status=ValidationStatus.uncertain, confidence=0.55, reason="?", severity=Severity.medium),
    )
    d, _ = _apply_matrix(rep, cost_budget_exceeded=False, matrix=matrix)
    assert d == Decision.human_review


def test_cost_budget_exceeded_routes_to_human_review(matrix):
    rep = _report(
        RuleResult(rule_id="a", status=ValidationStatus.match, confidence=0.99, reason="ok", severity=Severity.low),
    )
    d, _ = _apply_matrix(rep, cost_budget_exceeded=True, matrix=matrix)
    assert d == Decision.human_review


def test_low_confidence_routes_to_human_review(matrix):
    rep = _report(
        RuleResult(rule_id="a", status=ValidationStatus.match, confidence=0.85, reason="ok", severity=Severity.low),
    )
    d, _ = _apply_matrix(rep, cost_budget_exceeded=False, matrix=matrix)
    assert d == Decision.human_review
