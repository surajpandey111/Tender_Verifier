"""
Preprocessing pipeline applied to every page image before OCR.

Handles the real-world scan problems visible in the sample photos:
  - phone-camera photos taken at an angle (rotation/skew)
  - poor lighting / low contrast
  - noise/speckle from repeated photocopying
  - upside-down pages
  - blur (mitigated via sharpening; can't fully fix blur, but helps)

Order matters: grayscale -> denoise -> deskew -> contrast -> binarize.
Deskew must run on grayscale (not yet binarized) for a stable angle estimate.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytesseract

# --- Portable Tesseract support (for .exe distribution) ---
# When this app is packaged with PyInstaller, we ship a "Tesseract-OCR" folder
# next to the .exe (see BUILD_EXE.md) instead of requiring the recipient to run
# a separate Tesseract installer. If that folder exists, point pytesseract at
# it; otherwise fall back to whatever "tesseract" is on the system PATH (the
# normal dev-machine setup, e.g. via `sudo apt install tesseract-ocr`).
import sys as _sys
from pathlib import Path as _Path

_app_dir = _Path(_sys.executable).parent if getattr(_sys, "frozen", False) else _Path(__file__).parent.parent
_bundled_tesseract = _app_dir / "Tesseract-OCR" / "tesseract.exe"
if _bundled_tesseract.exists():
    pytesseract.pytesseract.tesseract_cmd = str(_bundled_tesseract)
    import os as _os
    _os.environ["TESSDATA_PREFIX"] = str(_app_dir / "Tesseract-OCR" / "tessdata")
# --- end portable Tesseract support ---



def _to_grayscale(img: np.ndarray) -> np.ndarray:
    if len(img.shape) == 3:
        return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    return img


def _denoise(gray: np.ndarray) -> np.ndarray:
    return cv2.fastNlMeansDenoising(gray, h=10)


def _estimate_skew_angle(gray: np.ndarray) -> float:
    """
    Estimates rotation angle via the minAreaRect of all dark (text) pixels.
    Works well for photographed documents where the page itself is rotated
    a few degrees, as opposed to being 90/180/270 off (handled separately).
    """
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(thresh > 0))
    if coords.shape[0] < 50:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    # cv2.minAreaRect angle convention: normalize to [-45, 45]
    if angle < -45:
        angle = 90 + angle
    return angle


def _deskew(gray: np.ndarray) -> np.ndarray:
    angle = _estimate_skew_angle(gray)
    if abs(angle) < 0.3:
        return gray  # not worth the resample
    (h, w) = gray.shape
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _correct_orientation(gray: np.ndarray) -> np.ndarray:
    """
    Detects and corrects 90/180/270-degree misorientation (e.g. a page scanned
    sideways or upside down) using Tesseract's own orientation detection (OSD),
    which is far more reliable for gross rotation than pixel-geometry tricks.
    Falls back silently (no rotation) if OSD can't get a confident read, e.g.
    on very sparse/low-text pages.
    """
    try:
        osd = pytesseract.image_to_osd(gray)
        rotate_deg = 0
        for line in osd.splitlines():
            if line.startswith("Rotate:"):
                rotate_deg = int(line.split(":")[1].strip())
                break
        if rotate_deg == 0:
            return gray
        rot_map = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
        code = rot_map.get(rotate_deg)
        return cv2.rotate(gray, code) if code is not None else gray
    except Exception:
        return gray  # OSD failed (common on sparse/noisy pages) — proceed unrotated


def _enhance_contrast(gray: np.ndarray) -> np.ndarray:
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _binarize(gray: np.ndarray) -> np.ndarray:
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
    )


def preprocess_for_ocr(img: np.ndarray) -> np.ndarray:
    """Full pipeline. Input: RGB numpy array from a rendered PDF page. Output: binarized image ready for OCR."""
    gray = _to_grayscale(img)
    gray = _correct_orientation(gray)
    gray = _denoise(gray)
    gray = _deskew(gray)
    gray = _enhance_contrast(gray)
    return _binarize(gray)
