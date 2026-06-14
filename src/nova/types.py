"""Pydantic schemas crossing agent boundaries.

These are the typed JSON contracts described in PRD §5.3 / Appendix A–C.
Anything crossing an agent boundary lives here so the contracts are reviewable
in one place.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ─── shared enums ──────────────────────────────────────────────────────────


class DocType(str, Enum):
    bill_of_lading = "bill_of_lading"
    commercial_invoice = "commercial_invoice"
    packing_list = "packing_list"
    certificate_of_origin = "certificate_of_origin"


class ExtractionMethod(str, Enum):
    vlm_primary = "vlm_primary"
    vlm_retried = "vlm_retried"
    vlm_only_unverified = "vlm_only_unverified"  # PDF had no text layer to cross-check
    ocr_fallback = "ocr_fallback"


class ValidationStatus(str, Enum):
    match = "match"
    mismatch = "mismatch"
    uncertain = "uncertain"


class Decision(str, Enum):
    auto_approve = "auto_approve"
    human_review = "human_review"
    draft_amendment = "draft_amendment"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


# ─── A · Extractor output ──────────────────────────────────────────────────


class BBox(BaseModel):
    """Approximate page-relative bounding box. (page-relative for portability across renders.)"""
    page: int = Field(ge=1)
    # Coordinates are 0..1 fractions of page width/height so they survive re-rendering.
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(ge=0, le=1)
    h: float = Field(ge=0, le=1)


class ExtractedField(BaseModel):
    """One field pulled from the document. Source-grounded by construction."""
    value: str | float | int | None
    confidence: float = Field(ge=0, le=1)
    quote: str = Field(description="Exact text from the document supporting the value.")
    source: BBox | None = None  # may be None when we only have quote-based grounding


class ExtractedDoc(BaseModel):
    """A · Extractor output. The 8 brief-required fields are required; engine adds more freely."""
    model_config = ConfigDict(use_enum_values=True)

    doc_id: str
    doc_type: DocType
    # Required 8 (per take-home brief)
    consignee_name: ExtractedField
    hs_code: ExtractedField
    port_of_loading: ExtractedField
    port_of_discharge: ExtractedField
    incoterms: ExtractedField
    description_of_goods: ExtractedField
    gross_weight: ExtractedField  # value stored as string "1240.5 KG" so unit is preserved
    invoice_number: ExtractedField

    unreadable_fields: list[str] = Field(default_factory=list)
    extractor_version: str
    extraction_method: ExtractionMethod = ExtractionMethod.vlm_primary
    page_count: int = 1

    def field(self, name: str) -> ExtractedField:
        return getattr(self, name)

    def all_fields(self) -> dict[str, ExtractedField]:
        return {
            "consignee_name": self.consignee_name,
            "hs_code": self.hs_code,
            "port_of_loading": self.port_of_loading,
            "port_of_discharge": self.port_of_discharge,
            "incoterms": self.incoterms,
            "description_of_goods": self.description_of_goods,
            "gross_weight": self.gross_weight,
            "invoice_number": self.invoice_number,
        }


# ─── B · Validator output ──────────────────────────────────────────────────


class RuleResult(BaseModel):
    rule_id: str
    status: ValidationStatus
    confidence: float = Field(ge=0, le=1)
    found: str | None = None
    expected: str | None = None
    reason: str
    severity: Severity = Severity.medium
    field: str | None = None


class ValidationReport(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    doc_id: str
    rule_set_id: str
    results: list[RuleResult]
    validator_version: str

    def by_status(self, status: ValidationStatus) -> list[RuleResult]:
        return [r for r in self.results if r.status == status.value]

    @property
    def has_high_severity_mismatch(self) -> bool:
        return any(
            r.status == ValidationStatus.mismatch.value and r.severity == Severity.high.value
            for r in self.results
        )

    @property
    def has_any_uncertain(self) -> bool:
        return any(r.status == ValidationStatus.uncertain.value for r in self.results)

    @property
    def has_any_mismatch(self) -> bool:
        return any(r.status == ValidationStatus.mismatch.value for r in self.results)


# ─── C · Router output ─────────────────────────────────────────────────────


class Discrepancy(BaseModel):
    field: str
    found: str | None
    expected: str | None
    severity: Severity


class SuggestedAction(BaseModel):
    type: Literal["queue_for_ops", "send_amendment_draft", "auto_complete"]
    priority: Literal["low", "normal", "high"] = "normal"
    preload_fields: list[str] = Field(default_factory=list)
    amendment_draft: str | None = None


class RouterDecision(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    doc_id: str
    decision: Decision
    rationale: str
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    suggested_action: SuggestedAction
    router_version: str


# ─── cost ledger (for observability) ───────────────────────────────────────


class StageCost(BaseModel):
    stage: Literal["extractor", "validator", "router", "nl_query"]
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    started_at: datetime
    ended_at: datetime


# ─── full pipeline state (LangGraph) ───────────────────────────────────────


class PipelineState(BaseModel):
    """Shared state passed between LangGraph nodes."""
    shipment_id: str
    customer_id: str = "acme"
    pdf_path: str
    doc_type: DocType = DocType.bill_of_lading

    # Stage outputs (populated as graph progresses)
    extraction: ExtractedDoc | None = None
    validation: ValidationReport | None = None
    decision: RouterDecision | None = None

    # Bookkeeping
    costs: list[StageCost] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.costs)
