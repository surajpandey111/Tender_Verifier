"""
OCR engine wrapper.

Design goal (per user's requirement): "never rely on one OCR" — but also
"easily feasible on laptop computers" and free. So:

  - Tesseract is the baseline: free, fast, CPU-only, always required.
  - EasyOCR is used as a SECOND OPINION when installed (it's a heavier,
    torch-based dependency — CPU-only is fine but slower, so it's optional,
    not required). If a page's Tesseract confidence is low, we run EasyOCR
    too and pick whichever result is longer/more confident, or concatenate
    unique lines from both — this is what "if one misses, another detects"
    means in practice, without requiring a GPU or paid API.

This keeps the system runnable on a bare laptop with just `pip install
pytesseract` + the tesseract binary, while still supporting the two-engine
setup when the extra dependency is present.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pytesseract

try:
    import easyocr  # heavy optional dependency (torch-based)
    _EASYOCR_READER = easyocr.Reader(["en"], gpu=False)
    _EASYOCR_AVAILABLE = True
except Exception:
    _EASYOCR_READER = None
    _EASYOCR_AVAILABLE = False


LOW_CONFIDENCE_THRESHOLD = 60.0  # below this, worth trying the second engine


def _tesseract_ocr(img: np.ndarray) -> tuple[str, float]:
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config="--psm 6")
    words, confs = [], []
    for word, conf in zip(data["text"], data["conf"]):
        word = word.strip()
        if word:
            words.append(word)
            try:
                c = float(conf)
                if c >= 0:
                    confs.append(c)
            except ValueError:
                pass
    text = " ".join(words)
    avg_conf = sum(confs) / len(confs) if confs else 0.0
    return text, avg_conf


def _easyocr_ocr(img: np.ndarray) -> tuple[str, float]:
    results = _EASYOCR_READER.readtext(img, detail=1)
    if not results:
        return "", 0.0
    texts = [r[1] for r in results]
    confs = [r[2] * 100 for r in results]  # easyocr conf is 0-1
    return " ".join(texts), sum(confs) / len(confs)


def ocr_image(img: np.ndarray) -> tuple[str, float]:
    """
    Returns (text, confidence_0_100). Always succeeds with Tesseract alone;
    only escalates to EasyOCR (if installed) when Tesseract confidence is low,
    to avoid paying the runtime cost of a second engine on every page.
    """
    text, conf = _tesseract_ocr(img)

    if conf >= LOW_CONFIDENCE_THRESHOLD or not _EASYOCR_AVAILABLE:
        return text, conf

    alt_text, alt_conf = _easyocr_ocr(img)
    if not alt_text:
        return text, conf

    # Merge: keep whichever is more confident as primary, but append any
    # substantially different content from the other so downstream keyword
    # matching (classifier) still sees it even if word-order differs.
    if alt_conf > conf:
        return f"{alt_text}\n{text}", alt_conf
    return f"{text}\n{alt_text}", conf
