"""FastAPI app — the HTTP surface of the prototype.

POST /shipments          — upload a PDF, kick off pipeline, return shipment_id
GET  /shipments          — list recent
GET  /shipments/{id}     — current state + persisted artifacts
WS   /shipments/{id}/ws  — stream stage events as the graph runs
POST /query              — Behaviour D — NL question over the work-store
POST /overrides          — record a human override
GET  /samples/{name}     — serve a sample PDF for the UI
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nova.config import get_settings
from nova.orchestrator.graph import graph_session
from nova.store.models import init_sync
from nova.store.nl_query import NLQueryResult, answer
from nova.store.repo import Repo

log = structlog.get_logger()
app = FastAPI(title="Nova trade-doc — prototype", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local-dev only
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory pubsub from graph runner → WS handler.
# Each shipment_id maps to one asyncio.Queue. Keeps the demo simple.
_STREAMS: dict[str, asyncio.Queue] = {}


_STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("startup")
def _startup() -> None:
    init_sync()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((_STATIC_DIR / "index.html").read_text(encoding="utf-8"))


# ─── shipments ────────────────────────────────────────────────────────────


class ShipmentSummary(BaseModel):
    shipment_id: str
    customer_id: str
    state: str
    pdf_path: str
    created_at: str


@app.post("/shipments")
async def create_shipment(file: UploadFile, customer_id: str = "acme") -> dict[str, Any]:
    s = get_settings()
    s.artifacts_dir.mkdir(parents=True, exist_ok=True)
    shipment_id = str(uuid.uuid4())
    save_path = s.artifacts_dir / shipment_id / "input.pdf"
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(await file.read())

    Repo().create_shipment(shipment_id, customer_id=customer_id, pdf_path=str(save_path))
    _STREAMS[shipment_id] = asyncio.Queue()

    asyncio.create_task(_run_pipeline(shipment_id, customer_id, save_path))
    log.info("api.shipment.created", shipment_id=shipment_id, file=file.filename)
    return {"shipment_id": shipment_id, "customer_id": customer_id}


@app.get("/shipments")
def list_shipments() -> list[ShipmentSummary]:
    with Repo() as r:
        rows = r.list_shipments()
    return [
        ShipmentSummary(
            shipment_id=s.shipment_id, customer_id=s.customer_id, state=s.state,
            pdf_path=s.pdf_path, created_at=s.created_at.isoformat(),
        )
        for s in rows
    ]


@app.get("/shipments/{shipment_id}")
def get_shipment(shipment_id: str) -> dict[str, Any]:
    with Repo() as r:
        ship = r.get_shipment(shipment_id)
        if not ship:
            raise HTTPException(status_code=404)
        ex = r.latest_extraction(shipment_id)
        va = r.latest_validation(shipment_id)
        de = r.latest_decision(shipment_id)

    return {
        "shipment_id": ship.shipment_id,
        "customer_id": ship.customer_id,
        "state": ship.state,
        "pdf_path": ship.pdf_path,
        "created_at": ship.created_at.isoformat(),
        "extraction": ex.extracted_json if ex else None,
        "extraction_meta": (
            {
                "extraction_id": ex.extraction_id,
                "extractor_version": ex.extractor_version,
                "method": ex.extraction_method,
                "cost_usd": ex.cost_usd,
                "latency_ms": ex.latency_ms,
            } if ex else None
        ),
        "validation": va.results_json if va else None,
        "validation_meta": (
            {
                "validation_id": va.validation_id,
                "rule_set_id": va.rule_set_id,
                "validator_version": va.validator_version,
                "n_match": va.n_match,
                "n_mismatch": va.n_mismatch,
                "n_uncertain": va.n_uncertain,
            } if va else None
        ),
        "decision": (
            {
                "decision_id": de.decision_id,
                "decision": de.decision,
                "rationale": de.rationale,
                "discrepancies": de.discrepancies_json,
                "router_version": de.router_version,
            } if de else None
        ),
    }


@app.websocket("/shipments/{shipment_id}/ws")
async def ws(shipment_id: str, ws: WebSocket) -> None:
    await ws.accept()
    q = _STREAMS.get(shipment_id)
    if not q:
        await ws.send_json({"type": "error", "message": "no such shipment stream"})
        await ws.close()
        return
    try:
        while True:
            ev = await q.get()
            await ws.send_json(ev)
            if ev.get("type") == "final":
                break
    except WebSocketDisconnect:
        pass


# ─── overrides ────────────────────────────────────────────────────────────


class OverrideIn(BaseModel):
    decision_id: str
    operator_id: str = "ops@acme"
    new_decision: str  # auto_approve | human_review | draft_amendment
    rationale: str


@app.post("/overrides")
def create_override(body: OverrideIn) -> dict[str, Any]:
    with Repo() as r:
        row = r.save_override(
            decision_id=body.decision_id,
            operator_id=body.operator_id,
            new_decision=body.new_decision,
            rationale=body.rationale,
        )
    return {"override_id": row.override_id}


# ─── NL query (Behaviour D) ──────────────────────────────────────────────


class QueryIn(BaseModel):
    question: str


@app.post("/query")
async def query(body: QueryIn) -> NLQueryResult:
    try:
        return await answer(body.question)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


# ─── samples ──────────────────────────────────────────────────────────────


@app.get("/samples/{name}")
def get_sample(name: str) -> FileResponse:
    p = (get_settings().samples_dir / name).resolve()
    if not str(p).startswith(str(get_settings().samples_dir.resolve())):
        raise HTTPException(status_code=400, detail="bad path")
    if not p.exists():
        raise HTTPException(status_code=404)
    return FileResponse(str(p), media_type="application/pdf")


@app.get("/health")
def health() -> dict[str, Any]:
    s = get_settings()
    return {
        "status": "ok",
        "provider": s.llm_provider,
        "vision_model": s.effective_vision_model,
        "text_model": s.effective_text_model,
        "llm_endpoint": s.vision_endpoint,
    }


# ─── pipeline runner ──────────────────────────────────────────────────────


async def _run_pipeline(shipment_id: str, customer_id: str, pdf_path: Path) -> None:
    """Run the graph, streaming each stage event over the WS queue."""
    q = _STREAMS[shipment_id]
    initial: dict[str, Any] = {
        "shipment_id": shipment_id,
        "customer_id": customer_id,
        "pdf_path": str(pdf_path),
        "doc_type": "bill_of_lading",
        "costs": [],
        "errors": [],
        "stage_events": [],
    }
    last_emitted = 0
    config = {"configurable": {"thread_id": shipment_id}}
    try:
        async with graph_session() as graph:
            async for state in graph.astream(initial, config=config, stream_mode="values"):
                events = state.get("stage_events", [])
                new = events[last_emitted:]
                last_emitted = len(events)
                for ev in new:
                    await q.put({"type": "stage_event", **ev})
                await q.put({"type": "state", **{k: v for k, v in state.items() if k != "stage_events"}})
        await q.put({"type": "final", "shipment_id": shipment_id})
    except Exception as e:
        log.exception("pipeline.failed", err=str(e))
        await q.put({"type": "error", "message": str(e)})
        await q.put({"type": "final", "shipment_id": shipment_id})
