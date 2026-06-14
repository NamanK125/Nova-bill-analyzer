"""LangGraph shared state.

LangGraph needs a TypedDict (or Pydantic v1) for its reducer — we use TypedDict
so updates are dict-merges with no surprises. Pydantic types live on the wire
(see nova.types); they are serialised in and out of the TypedDict.
"""
from __future__ import annotations

from typing import Any, TypedDict


class GraphState(TypedDict, total=False):
    shipment_id: str
    customer_id: str
    pdf_path: str
    doc_type: str
    rule_set_id: str
    doc_schema: dict[str, Any]   # loaded YAML
    extraction: dict[str, Any] | None
    extraction_id: str | None
    validation: dict[str, Any] | None
    validation_id: str | None
    decision: dict[str, Any] | None
    decision_id: str | None
    costs: list[dict[str, Any]]
    errors: list[str]
    stage_events: list[dict[str, Any]]  # for WS streaming
