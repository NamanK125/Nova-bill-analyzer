"""The PDF synthesizer must produce a real PDF with the expected variant payload."""
from __future__ import annotations

from pathlib import Path

import pdfplumber

from nova.pdf.synth_bol import VARIANTS, synth


def test_synth_clean_contains_required_fields(tmp_path: Path):
    p = tmp_path / "clean.pdf"
    synth(p, "clean")
    assert p.exists() and p.stat().st_size > 1000
    with pdfplumber.open(str(p)) as pdf:
        text = pdf.pages[0].extract_text() or ""
    fields = VARIANTS["clean"]
    for k, v in fields.items():
        assert v.split(",")[0] in text, f"{k}={v!r} not present"


def test_mismatch_variant_has_unapproved_hs_code(tmp_path: Path):
    p = tmp_path / "mismatch.pdf"
    synth(p, "mismatch")
    with pdfplumber.open(str(p)) as pdf:
        text = pdf.pages[0].extract_text() or ""
    # The mismatch variant uses 8471.30 — explicitly outside ACME's approved list.
    assert "8471.30" in text
