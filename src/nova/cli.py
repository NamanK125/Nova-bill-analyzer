"""Headless runner — `python -m nova run <pdf>`.

Useful as a smoke test, for the take-home video, and as the thing CI calls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

import structlog

from nova.orchestrator.graph import graph_session
from nova.store.models import init_sync
from nova.store.repo import Repo

log = structlog.get_logger()


def _print_section(title: str) -> None:
    print()
    print("─" * 80)
    print(title)
    print("─" * 80)


async def run_pipeline(pdf_path: Path, customer: str) -> None:
    init_sync()
    shipment_id = str(uuid.uuid4())
    Repo().create_shipment(shipment_id, customer_id=customer, pdf_path=str(pdf_path))

    initial: dict = {
        "shipment_id": shipment_id,
        "customer_id": customer,
        "pdf_path": str(pdf_path),
        "doc_type": "bill_of_lading",
        "costs": [],
        "errors": [],
        "stage_events": [],
    }

    print(f"shipment_id = {shipment_id}")
    print(f"pdf         = {pdf_path}")
    print(f"customer    = {customer}")

    config = {"configurable": {"thread_id": shipment_id}}
    async with graph_session() as graph:
        final = await graph.ainvoke(initial, config=config)

    _print_section("STAGE EVENTS")
    for ev in final.get("stage_events", []):
        print(f"  [{ev['ts']}]  {ev['stage']:<24} {ev['status']}")

    _print_section("A · EXTRACTION")
    if final.get("extraction"):
        ex = final["extraction"]
        print(f"  doc_id: {ex['doc_id']}  method: {ex['extraction_method']}")
        print(f"  unreadable_fields: {ex['unreadable_fields']}")
        for fid in [
            "consignee_name","hs_code","port_of_loading","port_of_discharge",
            "incoterms","description_of_goods","gross_weight","invoice_number",
        ]:
            f = ex[fid]
            print(f"    {fid:<22}  value={f['value']!r:<40} conf={f['confidence']:.2f}  quote={f['quote'][:60]!r}")
    else:
        print("  (no extraction — see errors)")

    _print_section("B · VALIDATION")
    if final.get("validation"):
        for r in final["validation"]["results"]:
            tag = {"match":"✓ ", "mismatch":"✗ ", "uncertain":"? "}.get(r["status"], "?")
            print(f"  {tag} {r['rule_id']:<32} {r['status']:<10} sev={r['severity']:<6} conf={r['confidence']:.2f}")
            if r["status"] != "match":
                print(f"        found: {r.get('found')!r}")
                print(f"        expected: {r.get('expected')!r}")
                print(f"        reason: {r['reason']}")
    else:
        print("  (no validation)")

    _print_section("C · DECISION")
    if final.get("decision"):
        d = final["decision"]
        print(f"  decision: {d['decision']}")
        print(f"  rationale: {d['rationale']}")
        if d.get("discrepancies"):
            print("  discrepancies:")
            for x in d["discrepancies"]:
                print(f"    - {x['field']}: found {x['found']!r} vs expected {x['expected']!r} (sev={x['severity']})")
        sa = d.get("suggested_action") or {}
        if sa.get("amendment_draft"):
            print()
            print("  amendment draft (not sent):")
            print("  ──────────────────────────")
            for line in sa["amendment_draft"].splitlines():
                print(f"  | {line}")

    _print_section("LEDGER")
    total = sum(c["cost_usd"] for c in final.get("costs", []))
    total_lat = sum(c["latency_ms"] for c in final.get("costs", []))
    for c in final.get("costs", []):
        print(f"  {c['stage']:<10} {c['model']:<40} in={c['tokens_in']:<6} out={c['tokens_out']:<6} ${c['cost_usd']:.4f}  {c['latency_ms']}ms")
    print(f"  {'TOTAL':<10} {'':<40} {'':<7} {'':<7} ${total:.4f}  {total_lat}ms")

    if final.get("errors"):
        _print_section("ERRORS")
        for e in final["errors"]:
            print(f"  ! {e}")
        sys.exit(1)


def main() -> None:
    structlog.configure(processors=[structlog.processors.KeyValueRenderer()])
    ap = argparse.ArgumentParser(prog="nova")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run", help="run pipeline end-to-end on a PDF")
    rp.add_argument("pdf", type=Path)
    rp.add_argument("--customer", default="acme")
    args = ap.parse_args()
    if args.cmd == "run":
        asyncio.run(run_pipeline(args.pdf, args.customer))


if __name__ == "__main__":
    main()
