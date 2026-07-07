"""
Entry point. Point this at a folder of tender subfolders:

    tenders_root/
        TENDER_001/
            *.pdf                              (one or more files, any name)
            eligibility_rules.json              (copied from config/eligibility_rules_template.json, filled in)
        TENDER_002/
            ...

Run:
    python main.py --tenders-root /path/to/tenders_root --workers 12

Each tender folder is processed independently in its own worker process
(this is the parallelism that gets 400 tenders x 100-350 pages done in
hours, not days). Within a tender, pages are processed sequentially since
OCR/LLM calls per page are already fast relative to per-tender overhead;
cross-tender parallelism is where the real win is.

Output per tender: <tender_folder>/report.xlsx, <tender_folder>/report.pdf,
plus a row in storage/tender_verifier.db.
"""

from __future__ import annotations

import argparse
import glob
import multiprocessing
import os
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # reads .env from the current working directory (or wherever it's found up the tree)

sys.path.insert(0, str(Path(__file__).parent))  # allow `core.x` imports when run as a script

from core.classifier import Classifier
from core.extractor import Extractor
from core.groq_client import GroqKeyPool
from core.paths import CONFIG_DIR
from core.pdf_reader import iter_pdf_pages
from core.rule_engine import RuleEngine
from reports.report_generator import generate_excel_report, generate_pdf_report
from storage.db import init_db, save_tender_results


def build_groq_pool() -> GroqKeyPool | None:
    """Returns None (LLM fallback disabled, rules/regex-only mode) if no keys are configured —
    the pipeline must run without Groq at all, just with reduced recall on messy fields."""
    try:
        return GroqKeyPool()
    except ValueError:
        return None


def process_one_tender(tender_folder: str) -> dict:
    """
    Runs in a worker process. Returns a small summary dict (not the full data —
    keep inter-process messages small); the worker writes its own reports/DB
    row directly since SQLite WAL mode tolerates concurrent writers.
    """
    tender_id = Path(tender_folder).name
    groq_pool = build_groq_pool()
    classifier = Classifier(groq_pool=groq_pool)
    extractor = Extractor(groq_pool=groq_pool)

    # NOTE: glob is non-recursive by design here, so it will never descend into
    # the _output/ subfolder where this tender's own generated reports live.
    pdf_files = sorted(glob.glob(os.path.join(tender_folder, "*.pdf")))
    if not pdf_files:
        return {"tender_id": tender_id, "status": "ERROR", "detail": "No PDF files found in folder."}

    extracted_docs = []
    for pdf_path in pdf_files:
        source_file = Path(pdf_path).name
        try:
            for page in iter_pdf_pages(pdf_path):
                classification = classifier.classify_with_llm_fallback(page.text)
                if classification.doc_type is None:
                    continue  # unclassified page (blank, cover sheet, etc.) — not an error, just skip

                extraction = extractor.extract(page.text, classification.doc_type, page.page_number)

                extracted_docs.append({
                    "doc_type": classification.doc_type,
                    "label": classification.label,
                    "source_file": source_file,
                    "page_number": page.page_number,
                    "classification_method": classification.method,
                    "classification_score": classification.score,
                    "ocr_source": page.source,
                    "ocr_confidence": page.ocr_confidence,
                    "fields": extraction.fields,
                    "extraction_notes": extraction.extraction_notes,
                })
        except Exception as e:  # noqa: BLE001 — one bad PDF must not kill the whole batch
            extracted_docs.append({
                "doc_type": "ERROR", "label": "Processing Error", "source_file": source_file,
                "page_number": 0, "fields": {"error": str(e)}, "extraction_notes": {},
            })

    # Eligibility rules: per-tender file, falls back to the template (documents-found-only report,
    # no PASS/FAIL) if the tender folder doesn't have its own criteria file yet.
    rules_path = os.path.join(tender_folder, "eligibility_rules.json")
    if not os.path.exists(rules_path):
        rules_path = str(CONFIG_DIR / "eligibility_rules_template.json")

    rule_engine = RuleEngine(rules_path)
    clean_docs = [d for d in extracted_docs if d["doc_type"] != "ERROR"]
    verdict = rule_engine.evaluate(clean_docs)

    # Reports go in a subfolder, never directly in tender_folder — otherwise a re-run's
    # `glob(*.pdf)` would pick up last run's own report.pdf and try to classify it as
    # a submitted document (this happened during testing: the report's own text
    # "Experience / Work Completion Certificate" got matched as a real certificate).
    output_dir = os.path.join(tender_folder, "_output")
    os.makedirs(output_dir, exist_ok=True)
    excel_path = os.path.join(output_dir, "report.xlsx")
    pdf_path_out = os.path.join(output_dir, "report.pdf")
    generate_excel_report(tender_id, clean_docs, verdict, excel_path)
    generate_pdf_report(tender_id, clean_docs, verdict, pdf_path_out)

    save_tender_results(
        tender_id=tender_id,
        source_folder=tender_folder,
        processed_at=datetime.now().isoformat(),
        extracted_docs=clean_docs,
        verdict=verdict,
    )

    return {
        "tender_id": tender_id, "status": "OK", "overall_result": verdict.overall_status,
        "documents_found": len(clean_docs), "report_excel": excel_path, "report_pdf": pdf_path_out,
    }


def main():
    parser = argparse.ArgumentParser(description="Batch tender document verification pipeline.")
    parser.add_argument("--tenders-root", required=True, help="Folder containing one subfolder per tender.")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()

    init_db()

    tender_folders = sorted(
        p for p in glob.glob(os.path.join(args.tenders_root, "*")) if os.path.isdir(p)
    )
    print(f"Found {len(tender_folders)} tender folders. Processing with {args.workers} workers...")

    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_one_tender, folder): folder for folder in tender_folders}
        for future in as_completed(futures):
            folder = futures[future]
            result = None
            try:
                result = future.result()
            except Exception:  # noqa: BLE001
                result = {"tender_id": Path(folder).name, "status": "ERROR", "detail": traceback.format_exc()}
            results.append(result)
            status_str = result.get("overall_result", result["status"])
            print(f"  [{len(results)}/{len(tender_folders)}] {result['tender_id']}: {status_str}")
            if result["status"] == "ERROR":
                print(result.get("detail", "(no further detail)"))

    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"\nDone. {ok}/{len(tender_folders)} tenders processed successfully.")
    print("Per-tender reports written as report.xlsx / report.pdf inside each tender folder.")
    print("All structured data also saved to storage/tender_verifier.db (queryable via storage/db.py).")


if __name__ == "__main__":
    multiprocessing.freeze_support()  # required for ProcessPoolExecutor to work inside a PyInstaller .exe
    main()
