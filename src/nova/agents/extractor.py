"""A · Extractor agent.

Vision-LLM call against a vLLM endpoint. Pulls the 8 brief-required fields with
per-field confidence + supporting quote. Quote is then verified against the
PDF text layer to demote hallucinated confidence.

Failure modes handled here:
 - Hallucinated field values: the quote-verification pass demotes confidence
   when the model's quote can't be found in the PDF text layer.
 - Unreadable fields: explicit `unreadable_fields` channel; never silent omission.
 - Confidence-floor: anything below `CONFIDENCE_FLOOR` is downgraded such that
   the validator will mark dependent rules as `uncertain`, never `match`.
"""
from __future__ import annotations

import difflib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pdfplumber
import structlog
import yaml
from pydantic import BaseModel, Field

from nova.config import get_settings
from nova.llm import LLM
from nova.pdf.render import render_pdf_to_pngs
from nova.types import (
    BBox,
    DocType,
    ExtractedDoc,
    ExtractedField,
    ExtractionMethod,
    StageCost,
)

log = structlog.get_logger()


# ─── intermediate Pydantic schemas the model is asked to fill ─────────────


class _FieldPayload(BaseModel):
    """What the vision model returns for one field."""
    value: str = Field(description="The value as it appears on the document.")
    confidence: float = Field(ge=0, le=1, description="Self-reported confidence.")
    quote: str = Field(description="Exact span from the document supporting the value.")


class _ExtractorPayload(BaseModel):
    """What the vision model returns overall."""
    consignee_name: _FieldPayload
    hs_code: _FieldPayload
    port_of_loading: _FieldPayload
    port_of_discharge: _FieldPayload
    incoterms: _FieldPayload
    description_of_goods: _FieldPayload
    gross_weight: _FieldPayload
    invoice_number: _FieldPayload
    unreadable_fields: list[str] = Field(default_factory=list)


# ─── extractor ────────────────────────────────────────────────────────────


_REQUIRED_FIELDS = [
    "consignee_name", "hs_code", "port_of_loading", "port_of_discharge",
    "incoterms", "description_of_goods", "gross_weight", "invoice_number",
]


def _load_doc_type_schema(doc_type: DocType) -> dict[str, Any]:
    s = get_settings()
    path = s.schemas_dir / "doc_types" / f"{doc_type.value if hasattr(doc_type,'value') else doc_type}.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def _build_extractor_prompt(schema: dict[str, Any]) -> tuple[str, str]:
    """Build the (system, user) prompt pair from the YAML doc-type schema."""
    sys = (
        "You are a trade-document extraction agent. You read scanned and digital "
        "Bills of Lading and return ONE structured JSON object. You never invent "
        "fields, and you never guess at characters you cannot actually see.\n\n"
        "Rules you MUST follow — these are load-bearing:\n"
        "  1. EVERY value you return must come from glyphs you can ACTUALLY READ on "
        "     the page image. Do not infer values from context, conventions, "
        "     surrounding text, or what you'd expect a BoL to say.\n"
        "  2. If ANY portion of a field's glyphs is occluded, blacked-out, "
        "     smudged, faded, cut off, ink-blotted, or otherwise visually obscured "
        "     — even partially — you MUST:\n"
        "        (a) put the field id in `unreadable_fields`, AND\n"
        "        (b) set its confidence to 0.4 or lower, AND\n"
        "        (c) set its value to whatever fragment is legible OR to the empty "
        "            string if nothing is legible.\n"
        "     Guessing the obscured digits/characters from context is the single "
        "     most expensive mistake you can make. A field with a visible 'KG' but "
        "     blacked-out digits is UNREADABLE, not '1240.5 KG'.\n"
        "  3. The `quote` MUST be an exact substring of the visible document text. "
        "     If you cannot find the substring on the page, the field is "
        "     unreadable (rule 2 applies).\n"
        "  4. Confidence is your honest probability that the value is correct. Do "
        "     not anchor on 0.9 or 0.97. If a value is hard to read, say 0.6. If "
        "     something is partially occluded, say 0.4 and mark it unreadable. If "
        "     a value is crisp and unambiguous, 0.95+ is fine.\n"
    )

    field_hints = "\n".join(
        f"  - {f['id']}: {f.get('hint','')}" for f in schema["fields"]
    )
    user = (
        "Extract the following fields from the Bill of Lading image(s). "
        "Return one JSON object with each field as `{value, confidence, quote}` "
        "plus an `unreadable_fields` array.\n\n"
        f"Fields:\n{field_hints}\n\n"
        "Reminder: `quote` must be an exact substring of the document text."
    )
    return sys, user


async def extract(pdf_path: Path, *, doc_id: str | None = None, doc_type: DocType = DocType.bill_of_lading) -> tuple[ExtractedDoc, list[StageCost]]:
    """Run the Extractor on one PDF. Returns (ExtractedDoc, cost ledger)."""
    s = get_settings()
    doc_id = doc_id or str(uuid.uuid4())
    log.info("extractor.start", doc_id=doc_id, pdf=str(pdf_path))

    # Render pages to PNGs for the VLM.
    img_dir = s.artifacts_dir / doc_id / "pages"
    pngs = render_pdf_to_pngs(pdf_path, img_dir)
    log.info("extractor.pages_rendered", doc_id=doc_id, n_pages=len(pngs))

    # Build prompt from doc-type schema.
    schema = _load_doc_type_schema(doc_type)
    sys_prompt, user_prompt = _build_extractor_prompt(schema)

    # Vision call.
    llm = LLM()
    payload, cost = await llm.vision_json(
        system=sys_prompt,
        user_text=user_prompt,
        image_paths=pngs,
        schema=_ExtractorPayload,
        temperature=s.extractor_temperature,
        max_tokens=s.extractor_max_tokens,
        stage="extractor",
    )
    costs = [cost]
    assert isinstance(payload, _ExtractorPayload)

    # Quote-verification: tolerant cross-check of extracted values against the
    # PDF text layer. Three regimes:
    #   1. Text layer present (digital PDF) → value or quote must match (exact
    #      substring or fuzzy ratio ≥ 0.85). If neither, demote confidence.
    #   2. No text layer (scanned PDF) → we *cannot* cross-check; mark the
    #      extraction method as vlm_only_unverified rather than blindly
    #      demoting every field. The downstream router still sees the model's
    #      self-reported confidence, which the validator's accept-threshold guards.
    #   3. ocr_fallback path (future) → text comes from PaddleOCR; same logic.
    text_layer = _pdf_text(pdf_path)
    has_text_layer = _has_usable_text_layer(text_layer)
    demoted: list[str] = []
    final_fields: dict[str, ExtractedField] = {}
    for fid in _REQUIRED_FIELDS:
        fp: _FieldPayload = getattr(payload, fid)
        value_str = str(fp.value) if fp.value is not None else ""
        conf = fp.confidence
        unreadable = fid in payload.unreadable_fields
        if has_text_layer:
            verified = _value_supported_by_text(value_str, fp.quote, text_layer)
            if not verified and not unreadable:
                demoted.append(fid)
                conf = min(conf, 0.55)
                log.warning(
                    "extractor.value_unverified",
                    doc_id=doc_id, field=fid, value=value_str[:60], quote=fp.quote[:60],
                )
        # When there's no text layer, we trust the VLM's self-reported confidence
        # and let the validator's accept-threshold decide. This is logged below.

        if conf < s.confidence_floor:
            # below floor → still surface the value but flag for review
            if fid not in payload.unreadable_fields:
                payload.unreadable_fields.append(fid)

        final_fields[fid] = ExtractedField(
            value=fp.value,
            confidence=conf,
            quote=fp.quote,
            source=None,  # PDF-text-layer bbox attempted client-side in render module; left None here
        )

    method: ExtractionMethod
    if not has_text_layer:
        method = ExtractionMethod.vlm_only_unverified
    elif demoted:
        method = ExtractionMethod.vlm_retried
    else:
        method = ExtractionMethod.vlm_primary

    # System-policy guard: when we have NO ground truth (no PDF text layer, no
    # OCR pass yet), VLM self-reported confidence is unreliable — small open
    # VLMs are over-confident and will hallucinate plausible values for
    # obscured glyphs. Until we wire an OCR cross-check or a calibration
    # network, we cap unverifiable confidences below the accept threshold so
    # the validator marks every rule as `uncertain` and the router escalates.
    # See ADR 005 — "never silently approve" applied at the system layer
    # rather than relying on the model to admit uncertainty.
    if method == ExtractionMethod.vlm_only_unverified:
        cap = min(s.confidence_accept - 0.05, 0.90)
        for fid, field in final_fields.items():
            if field.confidence > cap:
                final_fields[fid] = ExtractedField(
                    value=field.value, confidence=cap,
                    quote=field.quote, source=field.source,
                )
        log.info("extractor.unverified_confidence_capped", doc_id=doc_id, cap=cap)

    extracted = ExtractedDoc(
        doc_id=doc_id,
        doc_type=doc_type,
        consignee_name=final_fields["consignee_name"],
        hs_code=final_fields["hs_code"],
        port_of_loading=final_fields["port_of_loading"],
        port_of_discharge=final_fields["port_of_discharge"],
        incoterms=final_fields["incoterms"],
        description_of_goods=final_fields["description_of_goods"],
        gross_weight=final_fields["gross_weight"],
        invoice_number=final_fields["invoice_number"],
        unreadable_fields=sorted(set(payload.unreadable_fields)),
        extractor_version=f"{s.vision_model}@2026-05",
        extraction_method=method,
        page_count=len(pngs),
    )
    log.info(
        "extractor.done",
        doc_id=doc_id,
        n_unreadable=len(extracted.unreadable_fields),
        cost_usd=round(sum(c.cost_usd for c in costs), 4),
        latency_ms=sum(c.latency_ms for c in costs),
    )
    return extracted, costs


# ─── helpers ───────────────────────────────────────────────────────────────


def _pdf_text(pdf_path: Path) -> str:
    """Return the concatenated visible text of the PDF (text layer)."""
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:
        log.warning("pdf_text.failed", err=str(e))
        return ""


def _norm(s: str) -> str:
    """Whitespace-collapse + lowercase. Punctuation kept; PDF-text-layer
    extractors are inconsistent about it but so are humans."""
    return " ".join(s.split()).lower()


def _quote_in_text(quote: str, text: str) -> bool:
    """Exact substring check after normalisation."""
    if not quote or not text:
        return False
    return _norm(quote) in _norm(text)


def _has_usable_text_layer(text: str) -> bool:
    """Heuristic: a scanned PDF returns nothing or a few stray glyphs from
    pdfplumber. Digital PDFs return hundreds of chars."""
    return len(text.strip()) >= 40


def _value_supported_by_text(value: str, quote: str, text: str) -> bool:
    """Tolerant verification: the extracted value (or its supporting quote) is
    supported by the PDF text if either matches exactly OR a windowed
    similarity ratio meets the threshold.

    Why two channels: VLMs sometimes echo the field label in their quote
    ('HS CODE\\n8471.41'), which won't substring-match a PDF text layer that
    concatenates labels across columns. The *value* itself is the load-bearing
    signal; we check it first.
    """
    if not text:
        return False
    nv = _norm(value).strip()
    nq = _norm(quote).strip()
    nt = _norm(text)

    # Fast path: exact substring on either channel.
    if nv and nv in nt:
        return True
    if nq and nq in nt:
        return True

    # Fuzzy path: slide a window the size of the value across the text and
    # take the best ratio. Word-step windows keep this linear-ish.
    target = nv if len(nv) >= 3 else nq
    if not target:
        return False
    L = max(len(target), 4)
    words = nt.split()
    if not words:
        return False
    best = 0.0
    # Step word-by-word; window covers up to twice the target length so the
    # comparator sees enough context.
    for i in range(len(words)):
        window = " ".join(words[i:i + 8])[: L * 2]
        if not window:
            continue
        ratio = difflib.SequenceMatcher(None, target, window).ratio()
        if ratio > best:
            best = ratio
            if best >= 0.85:
                return True
    return best >= 0.85
