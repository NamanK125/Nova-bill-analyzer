"""Deterministic checks of the Validator — no LLM, no network.

Covers the three rule types (membership, regex, presence) and the
'never silently approve' behaviour when the underlying extraction is uncertain.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nova.agents.validator import validate
from nova.types import (
    DocType,
    ExtractedDoc,
    ExtractedField,
    ExtractionMethod,
    ValidationStatus,
)


def _ef(value: str, conf: float = 0.99) -> ExtractedField:
    return ExtractedField(value=value, confidence=conf, quote=value, source=None)


def _good_doc(**overrides) -> ExtractedDoc:
    fields = {
        "consignee_name": _ef("ACME Pharma Ltd"),
        "hs_code": _ef("8471.41"),
        "port_of_loading": _ef("Shenzhen"),
        "port_of_discharge": _ef("Rotterdam"),
        "incoterms": _ef("FOB Shenzhen"),
        "description_of_goods": _ef("Laptop computers, model X1, palletised"),
        "gross_weight": _ef("1240.5 KG"),
        "invoice_number": _ef("INV-2026-00441"),
    }
    fields.update(overrides)
    return ExtractedDoc(
        doc_id="t-1",
        doc_type=DocType.bill_of_lading,
        unreadable_fields=[],
        extractor_version="test",
        extraction_method=ExtractionMethod.vlm_primary,
        **fields,
    )


@pytest.mark.asyncio
async def test_clean_doc_all_match():
    rep, _ = await validate(_good_doc())
    statuses = {r.rule_id: r.status for r in rep.results}
    assert all(s == ValidationStatus.match.value for s in statuses.values()), statuses


@pytest.mark.asyncio
async def test_hs_code_not_in_list_is_mismatch():
    doc = _good_doc(hs_code=_ef("8471.30"))
    rep, _ = await validate(doc)
    rule = next(r for r in rep.results if r.rule_id == "hs_code_in_approved_list")
    assert rule.status == ValidationStatus.mismatch.value
    assert "8471.30" in (rule.found or "")
    assert rule.severity == "high"


@pytest.mark.asyncio
async def test_low_confidence_forces_uncertain():
    """Confidence below accept threshold → uncertain, even if the value would match."""
    doc = _good_doc(hs_code=_ef("8471.41", conf=0.62))
    rep, _ = await validate(doc)
    rule = next(r for r in rep.results if r.rule_id == "hs_code_in_approved_list")
    assert rule.status == ValidationStatus.uncertain.value


@pytest.mark.asyncio
async def test_unreadable_field_is_uncertain():
    doc = _good_doc()
    doc.unreadable_fields = ["gross_weight"]
    rep, _ = await validate(doc)
    rule = next(r for r in rep.results if r.rule_id == "gross_weight_positive_with_unit")
    assert rule.status == ValidationStatus.uncertain.value


@pytest.mark.asyncio
async def test_invoice_number_format_mismatch():
    doc = _good_doc(invoice_number=_ef("12345"))
    rep, _ = await validate(doc)
    rule = next(r for r in rep.results if r.rule_id == "invoice_number_format")
    assert rule.status == ValidationStatus.mismatch.value


@pytest.mark.asyncio
async def test_incoterms_first_token_extraction():
    """'FOB Shenzhen' should pass against the allow-list because we take the first token."""
    doc = _good_doc(incoterms=_ef("FOB Shenzhen"))
    rep, _ = await validate(doc)
    rule = next(r for r in rep.results if r.rule_id == "incoterms_allowed")
    assert rule.status == ValidationStatus.match.value
