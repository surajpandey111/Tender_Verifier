"""
Run this FIRST on any new laptop before touching main.py:

    python check_setup.py

Prints a clear PASS/FAIL for every dependency, so you know exactly what to
fix before running the real pipeline — instead of main.py crashing halfway
through a 400-tender batch with a confusing error.
"""

import shutil
import subprocess
import sys

CHECKS_FAILED = []


def check(label: str, fn):
    try:
        detail = fn()
        print(f"  [OK]   {label}" + (f" — {detail}" if detail else ""))
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] {label} — {e}")
        CHECKS_FAILED.append(label)


def check_python_version():
    v = sys.version_info
    if v < (3, 10):
        raise RuntimeError(f"Python {v.major}.{v.minor} found — need 3.10+")
    return f"Python {v.major}.{v.minor}.{v.micro}"


def check_tesseract_binary():
    path = shutil.which("tesseract")
    if not path:
        raise RuntimeError(
            "tesseract binary not found on PATH. Install it:\n"
            "    Windows: https://github.com/UB-Mannheim/tesseract/wiki (installer, then add to PATH)\n"
            "    Mac:     brew install tesseract\n"
            "    Linux:   sudo apt install tesseract-ocr"
        )
    result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    return result.stdout.splitlines()[0] if result.stdout else path


def check_package(module_name: str, pip_name: str = None):
    def _fn():
        __import__(module_name)
        return f"pip install {pip_name or module_name}"
    return _fn


def check_groq_key():
    from dotenv import load_dotenv
    import os
    load_dotenv()
    keys = os.environ.get("GROQ_API_KEYS", "")
    if not keys.strip() or "your_first_key_here" in keys:
        raise RuntimeError(
            "No real Groq key set in .env — LLM fallback will be disabled "
            "(pipeline still runs, but with reduced field extraction). "
            "Copy .env.example to .env and add a real key from console.groq.com"
        )
    n = len([k for k in keys.split(",") if k.strip()])
    return f"{n} key(s) configured"


def check_groq_connection():
    from core.groq_client import GroqKeyPool
    pool = GroqKeyPool()
    result = pool.complete_json(
        system_prompt="Reply with JSON only.",
        user_prompt='Return this exact JSON: {"status": "ok"}',
        max_tokens=20,
    )
    if not result or result.get("status") != "ok":
        raise RuntimeError("Groq API did not respond as expected — check key validity/network.")
    return "reached Groq API and got a valid response"


def check_smoke_test():
    """Runs classification on a known sample string, without touching any real files."""
    from core.classifier import Classifier
    clf = Classifier(groq_pool=None)
    sample_text = "ANNUAL TURNOVER CERTIFICATE Chartered Accountants Membership UDIN Financial Year"
    result = clf.classify_rule_based(sample_text)
    if result.doc_type != "TURNOVER_CERTIFICATE":
        raise RuntimeError(f"Expected TURNOVER_CERTIFICATE, got {result.doc_type} — check config/document_rules.json")
    return f"classified correctly as {result.doc_type} (score {result.score})"


def main():
    print("\n=== 1. Python & OS-level tools ===")
    check("Python version", check_python_version)
    check("Tesseract OCR binary", check_tesseract_binary)

    print("\n=== 2. Python packages (pip install -r requirements.txt) ===")
    check("PyMuPDF (fitz)", check_package("fitz", "pymupdf"))
    check("pytesseract", check_package("pytesseract"))
    check("opencv-python-headless", check_package("cv2", "opencv-python-headless"))
    check("numpy", check_package("numpy"))
    check("Pillow", check_package("PIL", "Pillow"))
    check("openpyxl", check_package("openpyxl"))
    check("reportlab", check_package("reportlab"))
    check("groq", check_package("groq"))
    check("python-dotenv", check_package("dotenv", "python-dotenv"))

    print("\n=== 3. Groq API key (.env) — optional but recommended ===")
    check("GROQ_API_KEYS configured in .env", check_groq_key)
    if "GROQ_API_KEYS configured in .env" not in CHECKS_FAILED:
        check("Groq API reachable with your key", check_groq_connection)

    print("\n=== 4. Pipeline smoke test (no real files touched) ===")
    check("Classifier correctly identifies a sample document", check_smoke_test)

    print()
    if CHECKS_FAILED:
        print(f"RESULT: {len(CHECKS_FAILED)} check(s) failed: {', '.join(CHECKS_FAILED)}")
        print("Fix the [FAIL] items above before running main.py on real tenders.")
        sys.exit(1)
    else:
        print("RESULT: All checks passed. You're ready to run main.py.")
        sys.exit(0)


if __name__ == "__main__":
    main()
