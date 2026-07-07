"""
Reads a PDF page-by-page and returns text for each page, using the fastest
reliable method available for that page:

  1. Native text layer (PyMuPDF) — instant, perfect accuracy, no OCR needed.
     Many GeM/GST/govt-portal PDFs are "born digital" and have this.
  2. If a page has no usable text layer (scanned/photographed page), render
     it to an image, preprocess (deskew, denoise, contrast, binarize), and
     hand off to the OCR engine.

This is the first stage of the pipeline and the reason we don't OCR
60,000 pages when we don't have to — only the genuinely scanned ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

import fitz  # PyMuPDF
import numpy as np
from PIL import Image

from ocr.preprocess import preprocess_for_ocr
from ocr.engine import ocr_image


MIN_NATIVE_TEXT_CHARS = 20  # below this, treat the page as "no usable text layer"


@dataclass
class PageResult:
    page_number: int          # 1-indexed, matches what a human sees in the PDF viewer
    text: str
    source: str                # "native" | "ocr"
    ocr_confidence: Optional[float] = None  # 0-100, only set when source == "ocr"


def render_page_to_image(page: "fitz.Page", zoom: float = 2.5) -> Image.Image:
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return img


def iter_pdf_pages(pdf_path: str) -> Iterator[PageResult]:
    """
    Yields one PageResult per page. Generator so the caller (worker process)
    can stream pages instead of holding a whole 350-page doc's text in memory.
    """
    doc = fitz.open(pdf_path)
    try:
        for i, page in enumerate(doc, start=1):
            native_text = page.get_text("text").strip()

            if len(native_text) >= MIN_NATIVE_TEXT_CHARS:
                yield PageResult(page_number=i, text=native_text, source="native")
                continue

            # Fall back to OCR: render -> preprocess -> OCR
            img = render_page_to_image(page)
            processed = preprocess_for_ocr(np.array(img))
            ocr_text, confidence = ocr_image(processed)
            yield PageResult(page_number=i, text=ocr_text, source="ocr", ocr_confidence=confidence)
    finally:
        doc.close()


def get_page_count(pdf_path: str) -> int:
    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()
