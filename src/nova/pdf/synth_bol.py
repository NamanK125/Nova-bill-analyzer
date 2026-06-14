"""Synthesise an ACME Bill of Lading PDF.

Three variants for the demo + eval:
  clean      — all fields valid, all rules pass → auto_approve
  mismatch   — HS code outside ACME's approved list → human_review (high severity)
  uncertain  — gross_weight printed in low-contrast italics so the VLM is unsure
"""
from __future__ import annotations

import argparse
from pathlib import Path

import io

from pdf2image import convert_from_path
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas


# Per-variant field values. The 8 brief-required fields.
VARIANTS: dict[str, dict[str, str]] = {
    "clean": {
        "consignee_name": "ACME Pharma Ltd",
        "hs_code": "8471.41",
        "port_of_loading": "Shenzhen",
        "port_of_discharge": "Rotterdam",
        "incoterms": "FOB Shenzhen",
        "description_of_goods": "Laptop computers, model X1, palletised",
        "gross_weight": "1240.5 KG",
        "invoice_number": "INV-2026-00441",
    },
    "mismatch": {
        # HS code 8471.30 is NOT in ACME's approved list — seeds the demo mismatch.
        "consignee_name": "ACME Pharma Ltd",
        "hs_code": "8471.30",
        "port_of_loading": "Shenzhen",
        "port_of_discharge": "Rotterdam",
        "incoterms": "FOB Shenzhen",
        "description_of_goods": "Laptop computers, model X1, palletised",
        "gross_weight": "1240.5 KG",
        "invoice_number": "INV-2026-00442",
    },
    "uncertain": {
        # Real value is "1240.5 KG" — but the page renders it tiny + low-contrast
        # + occluded so the VLM hedges. We keep the value correct here; the
        # synth's rendering (see synth() below) is what does the obscuring.
        "consignee_name": "ACME Pharma Ltd",
        "hs_code": "8471.41",
        "port_of_loading": "Shenzhen",
        "port_of_discharge": "Rotterdam",
        "incoterms": "FOB Shenzhen",
        "description_of_goods": "Laptop computers, model X1, palletised",
        "gross_weight": "1240.5 KG",
        "invoice_number": "INV-2026-00443",
    },
}


def synth(out_path: Path, variant: str = "clean") -> None:
    fields = VARIANTS[variant]
    out_path.parent.mkdir(parents=True, exist_ok=True)

    c = Canvas(str(out_path), pagesize=A4)
    width, height = A4

    # ── header banner ────────────────────────────────────────────────
    c.setFillColor(colors.HexColor("#0B3D91"))
    c.rect(0, height - 25 * mm, width, 25 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(20 * mm, height - 14 * mm, "BILL OF LADING")
    c.setFont("Helvetica", 10)
    c.drawString(20 * mm, height - 21 * mm, "Carrier: Pacific Maritime Lines · Doc no. 2026/05/441")
    c.drawRightString(width - 20 * mm, height - 14 * mm, "ACME Pharma Ltd")
    c.drawRightString(width - 20 * mm, height - 21 * mm, "(Synthesised — demo use only)")

    # ── body box ────────────────────────────────────────────────────
    c.setFillColor(colors.black)
    c.setStrokeColor(colors.HexColor("#0B3D91"))
    c.setLineWidth(0.6)
    c.rect(15 * mm, 30 * mm, width - 30 * mm, height - 65 * mm, fill=0, stroke=1)

    def label(x_mm: float, y_mm: float, text: str) -> None:
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(colors.HexColor("#0B3D91"))
        c.drawString(x_mm * mm, y_mm * mm, text)
        c.setFillColor(colors.black)

    def value(
        x_mm: float, y_mm: float, text: str, *, font: str = "Helvetica", size: int = 12,
        color: colors.Color = colors.black,
    ) -> None:
        c.setFont(font, size)
        c.setFillColor(color)
        c.drawString(x_mm * mm, y_mm * mm, text)
        c.setFillColor(colors.black)

    # Left column
    label(20, 250, "CONSIGNEE")
    value(20, 244, fields["consignee_name"], font="Helvetica-Bold", size=12)

    label(20, 232, "INVOICE NUMBER")
    value(20, 226, fields["invoice_number"])

    label(20, 214, "DESCRIPTION OF GOODS")
    value(20, 208, fields["description_of_goods"])

    label(20, 196, "GROSS WEIGHT")
    if variant == "uncertain":
        # Simulate ink-smudge damage on a scanned doc: render the value, then
        # stamp opaque dark blots over several of the digits so the VLM can
        # neither read them confidently nor verify against the text layer.
        # This exercises the "never silently approve" guard — the VLM should
        # self-report confidence < 0.95 (or add gross_weight to unreadable_fields),
        # which the validator turns into `uncertain` → router routes to
        # human_review regardless of the regex outcome.
        value(20, 190, fields["gross_weight"], font="Helvetica-Bold", size=12)
        c.saveState()
        c.setFillColor(colors.black)  # fully opaque — alpha shenanigans let the VLM trace digits
        # One wide blot covering the entire numeric portion. Only " KG" stays visible.
        c.rect(19.5 * mm, 188.0 * mm, 12.5 * mm, 5.5 * mm, fill=1, stroke=0)
        c.restoreState()
    else:
        value(20, 190, fields["gross_weight"], font="Helvetica-Bold", size=12)

    # Right column
    label(110, 250, "HS CODE")
    value(110, 244, fields["hs_code"], font="Helvetica-Bold", size=14,
          color=(colors.HexColor("#B00020") if variant == "mismatch" else colors.black))

    label(110, 232, "PORT OF LOADING")
    value(110, 226, fields["port_of_loading"])

    label(110, 214, "PORT OF DISCHARGE")
    value(110, 208, fields["port_of_discharge"])

    label(110, 196, "INCOTERMS")
    value(110, 190, fields["incoterms"])

    # Footer notes
    c.setFont("Helvetica-Oblique", 8)
    c.setFillColor(colors.grey)
    c.drawString(20 * mm, 22 * mm, f"Synthesised by Nova prototype · variant={variant}")
    c.drawRightString(width - 20 * mm, 22 * mm, "Page 1 of 1")

    c.showPage()
    c.save()

    # For the uncertain variant we want a *scanned-looking* doc — no embedded
    # text layer for the verifier to rubber-stamp the VLM's output. We re-process
    # the freshly written PDF: rasterise → re-embed as image-only PDF. This is
    # the realistic "this is a scan" path the PRD's bad-scan fallback handles.
    if variant == "uncertain":
        _flatten_to_image_only(out_path)

    print(f"wrote {out_path}  (variant={variant})")


def _flatten_to_image_only(pdf_path: Path) -> None:
    """Re-write the PDF as a pure raster image (no text layer).

    Rationale: reportlab embeds the literal text glyphs in the PDF text layer
    even when we paint an opaque rectangle over them visually. That defeats
    every text-layer-based verification step the Extractor uses. A real scanned
    BoL has no such text layer — and that's the case we want the Extractor's
    `vlm_only_unverified` path to exercise.
    """
    pages = convert_from_path(str(pdf_path), dpi=200)
    if not pages:
        return
    first, *rest = pages
    first.save(
        str(pdf_path),
        save_all=bool(rest),
        append_images=rest,
        format="PDF",
        resolution=200,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--variant", choices=list(VARIANTS), default="clean")
    args = ap.parse_args()
    synth(args.out, args.variant)


if __name__ == "__main__":
    main()
