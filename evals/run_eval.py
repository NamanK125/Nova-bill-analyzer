"""Tiny eval harness.

For the take-home this runs against synthesised docs with deterministic
ground truth. In production this would point at a 500-doc human-labelled
golden set; the shape is the same.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

from nova.agents.extractor import extract
from nova.agents.validator import validate
from nova.pdf.synth_bol import VARIANTS, synth
from nova.types import DocType


async def run() -> None:
    out = Path("./data/eval")
    out.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}

    for variant in ["clean", "mismatch", "uncertain"]:
        pdf_path = out / f"acme_bol_{variant}.pdf"
        synth(pdf_path, variant)
        ex, _ = await extract(pdf_path, doc_id=f"eval-{variant}", doc_type=DocType.bill_of_lading)
        rep, _ = await validate(ex, rule_set_id="acme@v1")
        statuses = Counter(r.status for r in rep.results)
        results[variant] = {
            "n_match": statuses.get("match", 0),
            "n_mismatch": statuses.get("mismatch", 0),
            "n_uncertain": statuses.get("uncertain", 0),
            "unreadable_fields": ex.unreadable_fields,
            "per_field_conf": {k: round(v.confidence, 2) for k, v in ex.all_fields().items()},
        }

    print(json.dumps(results, indent=2))
    Path("./evals/report.md").write_text(_make_report(results), encoding="utf-8")


def _make_report(results: dict[str, dict]) -> str:
    lines = ["# Eval report\n",
             "Synthesised 3-variant golden set. Real golden set lives at `evals/golden/` in production.\n",
             "| variant | match | mismatch | uncertain | unreadable |",
             "|---|---|---|---|---|"]
    for k, v in results.items():
        lines.append(f"| {k} | {v['n_match']} | {v['n_mismatch']} | {v['n_uncertain']} | {len(v['unreadable_fields'])} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    asyncio.run(run())
