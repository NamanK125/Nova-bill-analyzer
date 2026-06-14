"""PDF → per-page PNGs for the vision model.

pdf2image hides poppler. We render at 200 DPI for legibility; large enough that
the VLM reads text reliably, small enough to stay under image-token budgets.
"""
from __future__ import annotations

from pathlib import Path

from pdf2image import convert_from_path
from PIL import Image


def render_pdf_to_pngs(pdf_path: Path, out_dir: Path, dpi: int = 200) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    images: list[Image.Image] = convert_from_path(str(pdf_path), dpi=dpi)
    paths: list[Path] = []
    for i, im in enumerate(images, start=1):
        p = out_dir / f"page-{i:02d}.png"
        im.save(p, "PNG")
        paths.append(p)
    return paths
