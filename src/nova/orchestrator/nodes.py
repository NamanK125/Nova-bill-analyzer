"""LangGraph node implementations — Nova's 5-stage pattern.

scope_resolution → context_compilation → schema_routing → plan_execute → evidence_delivery

Stages 1-3 + 5 are deterministic orchestrator code; stage 4 fans out to the
three agents. Each node returns a partial state dict that LangGraph merges into
the shared state. Every transition is logged to `stage_events` for the WS stream.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
import yaml

from nova.agents.extractor import extract
from nova.agents.router import route
from nova.agents.validator import validate
from nova.config import get_settings
from nova.orchestrator.state import GraphState
from nova.store.repo import Repo
from nova.types import (
    DocType,
    ExtractedDoc,
    RouterDecision,
    ValidationReport,
)

log = structlog.get_logger()


def _event(stage: str, status: str, **extra: Any) -> dict[str, Any]:
    return {"stage": stage, "status": status, "ts": datetime.utcnow().isoformat(), **extra}


# ─── 1 · scope resolution ──────────────────────────────────────────────────


def scope_resolution(state: GraphState) -> GraphState:
    """Identify customer, doc type, rule set version."""
    log.info("graph.scope_resolution", shipment_id=state["shipment_id"])
    customer = state.get("customer_id", "acme")
    doc_type = state.get("doc_type", DocType.bill_of_lading.value)
    rule_set_id = state.get("rule_set_id") or f"{customer}@v1"
    events = state.get("stage_events", []) + [_event("scope_resolution", "ok", customer=customer, doc_type=doc_type)]
    Repo().set_state(state["shipment_id"], "scoped")
    return {"customer_id": customer, "doc_type": doc_type, "rule_set_id": rule_set_id, "stage_events": events}


# ─── 2 · context compilation ───────────────────────────────────────────────


def context_compilation(state: GraphState) -> GraphState:
    """Pre-load anything the agents shouldn't fetch themselves (rules, history)."""
    log.info("graph.context_compilation", shipment_id=state["shipment_id"])
    # In v0 the validator loads its own ruleset; here we just verify it exists.
    s = get_settings()
    customer = state["customer_id"]
    rules_path = s.schemas_dir / "customers" / customer / "rules.yaml"
    if not rules_path.exists():
        err = f"ruleset for customer '{customer}' not found at {rules_path}"
        events = state.get("stage_events", []) + [_event("context_compilation", "error", error=err)]
        return {"errors": state.get("errors", []) + [err], "stage_events": events}
    events = state.get("stage_events", []) + [_event("context_compilation", "ok", rules_path=str(rules_path))]
    return {"stage_events": events}


# ─── 3 · schema routing ────────────────────────────────────────────────────


def schema_routing(state: GraphState) -> GraphState:
    """Pick the typed I/O schema for this doc type."""
    s = get_settings()
    path = s.schemas_dir / "doc_types" / f"{state['doc_type']}.yaml"
    with open(path) as f:
        doc_schema = yaml.safe_load(f)
    log.info("graph.schema_routing", shipment_id=state["shipment_id"], doc_type=state["doc_type"])
    events = state.get("stage_events", []) + [_event("schema_routing", "ok", schema_version=doc_schema.get("version", 1))]
    return {"doc_schema": doc_schema, "stage_events": events}


# ─── 4 · plan + execute (the three agents) ─────────────────────────────────


async def plan_execute_extract(state: GraphState) -> GraphState:
    sid = state["shipment_id"]
    log.info("graph.extract.start", shipment_id=sid)
    Repo().set_state(sid, "extracting")
    events = state.get("stage_events", []) + [_event("extractor", "started")]
    t0 = time.time()
    try:
        extracted, costs = await extract(
            Path(state["pdf_path"]),
            doc_id=sid,
            doc_type=DocType(state["doc_type"]),
        )
    except Exception as e:
        log.exception("graph.extract.failed", err=str(e))
        events.append(_event("extractor", "error", error=str(e)))
        return {"errors": state.get("errors", []) + [f"extractor: {e}"], "stage_events": events}

    # Persist
    with Repo() as r:
        ex_row = r.save_extraction(sid, extracted, costs)
    Repo().set_state(sid, "extracted")
    events.append(_event(
        "extractor", "ok",
        extraction_id=ex_row.extraction_id,
        n_unreadable=len(extracted.unreadable_fields),
        cost_usd=sum(c.cost_usd for c in costs),
        latency_ms=int((time.time() - t0) * 1000),
    ))
    return {
        "extraction": extracted.model_dump(mode="json"),
        "extraction_id": ex_row.extraction_id,
        "costs": state.get("costs", []) + [c.model_dump(mode="json") for c in costs],
        "stage_events": events,
    }


async def plan_execute_validate(state: GraphState) -> GraphState:
    sid = state["shipment_id"]
    if not state.get("extraction"):
        return {"errors": state.get("errors", []) + ["validator: no extraction"]}
    log.info("graph.validate.start", shipment_id=sid)
    Repo().set_state(sid, "validating")
    events = state.get("stage_events", []) + [_event("validator", "started")]
    extracted = ExtractedDoc.model_validate(state["extraction"])
    report, costs = await validate(extracted, rule_set_id=state["rule_set_id"])
    with Repo() as r:
        v_row = r.save_validation(sid, state["extraction_id"], report, costs)
    Repo().set_state(sid, "validated")
    events.append(_event(
        "validator", "ok",
        validation_id=v_row.validation_id,
        n_match=v_row.n_match, n_mismatch=v_row.n_mismatch, n_uncertain=v_row.n_uncertain,
    ))
    return {
        "validation": report.model_dump(mode="json"),
        "validation_id": v_row.validation_id,
        "costs": state.get("costs", []) + [c.model_dump(mode="json") for c in costs],
        "stage_events": events,
    }


async def plan_execute_route(state: GraphState) -> GraphState:
    sid = state["shipment_id"]
    if not state.get("validation") or not state.get("extraction"):
        return {"errors": state.get("errors", []) + ["router: missing inputs"]}
    log.info("graph.route.start", shipment_id=sid)
    Repo().set_state(sid, "routing")
    events = state.get("stage_events", []) + [_event("router", "started")]
    s = get_settings()
    extracted = ExtractedDoc.model_validate(state["extraction"])
    report = ValidationReport.model_validate(state["validation"])
    total_cost = sum(c["cost_usd"] for c in state.get("costs", []))
    decision, costs = await route(
        extracted, report,
        cost_budget_exceeded=total_cost > s.cost_budget_usd,
        customer_id=state["customer_id"],
    )
    with Repo() as r:
        d_row = r.save_decision(sid, state["validation_id"], decision, costs)
    Repo().set_state(sid, "routed")
    events.append(_event(
        "router", "ok",
        decision_id=d_row.decision_id, decision=decision.decision if isinstance(decision.decision, str) else decision.decision.value,
        n_discrepancies=len(decision.discrepancies),
    ))
    return {
        "decision": decision.model_dump(mode="json"),
        "decision_id": d_row.decision_id,
        "costs": state.get("costs", []) + [c.model_dump(mode="json") for c in costs],
        "stage_events": events,
    }


# ─── 5 · evidence delivery ─────────────────────────────────────────────────


def evidence_delivery(state: GraphState) -> GraphState:
    sid = state["shipment_id"]
    final_state = "completed" if not state.get("errors") else "failed"
    Repo().set_state(sid, final_state)
    events = state.get("stage_events", []) + [_event("evidence_delivery", "ok", final_state=final_state)]
    log.info("graph.evidence_delivery", shipment_id=sid, final_state=final_state)
    return {"stage_events": events}
