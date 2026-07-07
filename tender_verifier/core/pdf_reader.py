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


def render_page_to_image(page: "fitz.Page", zoom: float = 3.0) -> Image.Image:
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return img


def iter_pdf_pages(pdf_path: str) -> Iterator[PageResult]:
    """
    Yields one PageResult per page, strictly in order, one at a time.
    Kept for backward compatibility / simple scripts. For real batch runs,
    use read_pdf_pages_concurrent() instead — it's the same logic but runs
    OCR pages (the slow ones) in parallel instead of one-at-a-time.
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


# --- OCR render zoom -------------------------------------------------------
# 3.0 was overkill: a big chunk of the per-page cost (denoise, warp, CLAHE)
# scales with pixel count. 2.2 keeps text sharp enough for Tesseract on
# real scanned/portal-downloaded PDFs while meaningfully cutting per-page
# time.
OCR_RENDER_ZOOM = 2.2


def _ocr_one_page(pdf_path: str, page_number: int, zoom: float) -> PageResult:
    """Runs in a worker thread: opens its OWN fitz handle (fitz.Document is not
    safe to share across threads) so many OCR pages can render+OCR at once
    instead of one at a time. This is where most of the "why is this so slow"
    time actually goes, so it's the first thing worth parallelizing."""
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_number - 1]
        img = render_page_to_image(page, zoom=zoom)
    finally:
        doc.close()
    processed = preprocess_for_ocr(np.array(img))
    text, confidence = ocr_image(processed)
    return PageResult(page_number=page_number, text=text, source="ocr", ocr_confidence=confidence)


def read_pdf_pages_concurrent(pdf_path: str, ocr_workers: int = 4, zoom: float = OCR_RENDER_ZOOM) -> list[PageResult]:
    """
    Same result as iter_pdf_pages(), but as a list, and with OCR-needed pages
    processed CONCURRENTLY across a small thread pool instead of one at a
    time. Native-text pages (the fast path) are read up-front, sequentially
    (that part is already fast — no need to thread it).

    This is the main throughput fix for scanned/photographed bidder
    submissions: previously a 57-page PDF where every page needed OCR took
    ~57 x (render+denoise+OSD+deskew+tesseract) time, fully serial. With
    ocr_workers=4 the same PDF takes roughly a quarter of that wall-clock
    time (Tesseract's subprocess call and OSD both release the GIL while
    waiting, so threads help here even without full multiprocessing).
    """
    doc = fitz.open(pdf_path)
    try:
        page_count = doc.page_count
        results: list = [None] * page_count
        ocr_page_numbers = []
        for i in range(page_count):
            native_text = doc[i].get_text("text").strip()
            if len(native_text) >= MIN_NATIVE_TEXT_CHARS:
                results[i] = PageResult(page_number=i + 1, text=native_text, source="native")
            else:
                ocr_page_numbers.append(i + 1)
    finally:
        doc.close()

    if ocr_page_numbers:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=max(1, ocr_workers)) as pool:
            futures = {pool.submit(_ocr_one_page, pdf_path, pn, zoom): pn for pn in ocr_page_numbers}
            for future in as_completed(futures):
                pn = futures[future]
                results[pn - 1] = future.result()

    return results


def get_page_count(pdf_path: str) -> int:
    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()
