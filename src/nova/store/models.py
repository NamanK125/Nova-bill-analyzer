"""Work-store schema — PRD Appendix E.

Each agent output is a row. Every transition is auditable. The `overrides` table
is the System-of-Outcomes feedback signal made concrete.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from nova.config import get_settings


class Base(DeclarativeBase):
    pass


class Shipment(Base):
    __tablename__ = "shipments"
    shipment_id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String, index=True)
    state: Mapped[str] = mapped_column(String, index=True)  # ingested..completed|failed
    pdf_path: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    extractions: Mapped[list["Extraction"]] = relationship(back_populates="shipment")
    validations: Mapped[list["Validation"]] = relationship(back_populates="shipment")
    decisions: Mapped[list["DecisionRow"]] = relationship(back_populates="shipment")


class Extraction(Base):
    __tablename__ = "extractions"
    extraction_id: Mapped[str] = mapped_column(String, primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.shipment_id"), index=True)
    doc_type: Mapped[str] = mapped_column(String)
    extracted_json: Mapped[dict] = mapped_column(JSON)
    extractor_version: Mapped[str] = mapped_column(String)
    extraction_method: Mapped[str] = mapped_column(String)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    shipment: Mapped[Shipment] = relationship(back_populates="extractions")


class Validation(Base):
    __tablename__ = "validations"
    validation_id: Mapped[str] = mapped_column(String, primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.shipment_id"), index=True)
    extraction_id: Mapped[str] = mapped_column(ForeignKey("extractions.extraction_id"))
    rule_set_id: Mapped[str] = mapped_column(String, index=True)
    results_json: Mapped[dict] = mapped_column(JSON)
    validator_version: Mapped[str] = mapped_column(String)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    n_match: Mapped[int] = mapped_column(Integer, default=0)
    n_mismatch: Mapped[int] = mapped_column(Integer, default=0)
    n_uncertain: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    shipment: Mapped[Shipment] = relationship(back_populates="validations")


class DecisionRow(Base):
    __tablename__ = "decisions"
    decision_id: Mapped[str] = mapped_column(String, primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.shipment_id"), index=True)
    validation_id: Mapped[str] = mapped_column(ForeignKey("validations.validation_id"))
    decision: Mapped[str] = mapped_column(String, index=True)  # auto_approve | human_review | draft_amendment
    rationale: Mapped[str] = mapped_column(String)
    discrepancies_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    router_version: Mapped[str] = mapped_column(String)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    shipment: Mapped[Shipment] = relationship(back_populates="decisions")


class Override(Base):
    """Human override of a router decision. The feedback signal."""
    __tablename__ = "overrides"
    override_id: Mapped[str] = mapped_column(String, primary_key=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.decision_id"), index=True)
    operator_id: Mapped[str] = mapped_column(String)
    new_decision: Mapped[str] = mapped_column(String)
    rationale: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def init_sync(db_url: str | None = None) -> None:
    """Create tables in the configured SQLite DB. Idempotent."""
    s = get_settings()
    url = db_url or s.db_url_sync
    Path("data").mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, echo=False, future=True)
    Base.metadata.create_all(engine)
