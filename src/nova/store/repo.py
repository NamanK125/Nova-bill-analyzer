"""Typed repository for the work-store.

One method per artifact persistence — keeps the System-of-Outcomes substrate
explicit and easy to audit.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Iterable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from nova.config import get_settings
from nova.store.models import (
    DecisionRow,
    Extraction,
    Override,
    Shipment,
    Validation,
)
from nova.types import (
    ExtractedDoc,
    RouterDecision,
    StageCost,
    ValidationReport,
    ValidationStatus,
)


def _engine():
    s = get_settings()
    return create_engine(s.db_url_sync, echo=False, future=True)


_SessionLocal = sessionmaker(bind=_engine(), expire_on_commit=False)


class Repo:
    """Sync repo — fine for SQLite + a small demo. Async path lives in api/ws.py."""

    def __init__(self, session: Session | None = None) -> None:
        self._owns = session is None
        self.s = session or _SessionLocal()

    def __enter__(self) -> "Repo":
        return self

    def __exit__(self, *a) -> None:
        if self._owns:
            self.s.close()

    # ── shipments ────────────────────────────────────────────────────────

    def create_shipment(self, shipment_id: str, customer_id: str, pdf_path: str) -> Shipment:
        row = Shipment(
            shipment_id=shipment_id, customer_id=customer_id, state="ingested", pdf_path=pdf_path
        )
        self.s.add(row)
        self.s.commit()
        return row

    def set_state(self, shipment_id: str, state: str) -> None:
        row = self.s.get(Shipment, shipment_id)
        if row is None:
            return
        row.state = state
        row.updated_at = datetime.utcnow()
        self.s.commit()

    def get_shipment(self, shipment_id: str) -> Shipment | None:
        return self.s.get(Shipment, shipment_id)

    def list_shipments(self, limit: int = 50) -> list[Shipment]:
        return list(
            self.s.execute(select(Shipment).order_by(Shipment.created_at.desc()).limit(limit)).scalars()
        )

    # ── extractions ──────────────────────────────────────────────────────

    def save_extraction(self, shipment_id: str, doc: ExtractedDoc, costs: Iterable[StageCost]) -> Extraction:
        cs = list(costs)
        cost_usd = sum(c.cost_usd for c in cs)
        latency_ms = sum(c.latency_ms for c in cs)
        in_tok = sum(c.tokens_in for c in cs)
        out_tok = sum(c.tokens_out for c in cs)
        row = Extraction(
            extraction_id=str(uuid.uuid4()),
            shipment_id=shipment_id,
            doc_type=str(doc.doc_type) if not isinstance(doc.doc_type, str) else doc.doc_type,
            extracted_json=doc.model_dump(mode="json"),
            extractor_version=doc.extractor_version,
            extraction_method=str(doc.extraction_method) if not isinstance(doc.extraction_method, str) else doc.extraction_method,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            tokens_in=in_tok,
            tokens_out=out_tok,
        )
        self.s.add(row)
        self.s.commit()
        return row

    def latest_extraction(self, shipment_id: str) -> Extraction | None:
        return self.s.execute(
            select(Extraction)
            .where(Extraction.shipment_id == shipment_id)
            .order_by(Extraction.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    # ── validations ──────────────────────────────────────────────────────

    def save_validation(
        self,
        shipment_id: str,
        extraction_id: str,
        report: ValidationReport,
        costs: Iterable[StageCost],
    ) -> Validation:
        cs = list(costs)
        n_m = sum(1 for r in report.results if r.status == ValidationStatus.match.value)
        n_mm = sum(1 for r in report.results if r.status == ValidationStatus.mismatch.value)
        n_u = sum(1 for r in report.results if r.status == ValidationStatus.uncertain.value)
        row = Validation(
            validation_id=str(uuid.uuid4()),
            shipment_id=shipment_id,
            extraction_id=extraction_id,
            rule_set_id=report.rule_set_id,
            results_json=report.model_dump(mode="json"),
            validator_version=report.validator_version,
            cost_usd=sum(c.cost_usd for c in cs),
            latency_ms=sum(c.latency_ms for c in cs),
            n_match=n_m,
            n_mismatch=n_mm,
            n_uncertain=n_u,
        )
        self.s.add(row)
        self.s.commit()
        return row

    def latest_validation(self, shipment_id: str) -> Validation | None:
        return self.s.execute(
            select(Validation)
            .where(Validation.shipment_id == shipment_id)
            .order_by(Validation.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    # ── decisions ────────────────────────────────────────────────────────

    def save_decision(
        self,
        shipment_id: str,
        validation_id: str,
        decision: RouterDecision,
        costs: Iterable[StageCost],
    ) -> DecisionRow:
        cs = list(costs)
        row = DecisionRow(
            decision_id=str(uuid.uuid4()),
            shipment_id=shipment_id,
            validation_id=validation_id,
            decision=str(decision.decision) if not isinstance(decision.decision, str) else decision.decision,
            rationale=decision.rationale,
            discrepancies_json=[d.model_dump(mode="json") for d in decision.discrepancies] or None,
            router_version=decision.router_version,
            cost_usd=sum(c.cost_usd for c in cs),
            latency_ms=sum(c.latency_ms for c in cs),
        )
        self.s.add(row)
        self.s.commit()
        return row

    def latest_decision(self, shipment_id: str) -> DecisionRow | None:
        return self.s.execute(
            select(DecisionRow)
            .where(DecisionRow.shipment_id == shipment_id)
            .order_by(DecisionRow.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    # ── overrides ────────────────────────────────────────────────────────

    def save_override(
        self,
        decision_id: str,
        operator_id: str,
        new_decision: str,
        rationale: str,
    ) -> Override:
        row = Override(
            override_id=str(uuid.uuid4()),
            decision_id=decision_id,
            operator_id=operator_id,
            new_decision=new_decision,
            rationale=rationale,
        )
        self.s.add(row)
        self.s.commit()
        return row
