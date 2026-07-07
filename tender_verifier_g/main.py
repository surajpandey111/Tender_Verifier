"""
Entry point. Point this at a folder of TENDER folders, where each tender
folder contains one BIDDER subfolder per company that submitted a bid:

    tenders_root/
        UPKEEP/                                  <- one tender
            eligibility_rules.json                (optional, see README)
            1.AARUSHI SECURITY INFRA/              <- one bidder
                *.pdf                              (however many files that bidder submitted)
            2.AASTHA TECHNOLOGY SERVICES/
                *.pdf
            ...
        ANOTHER_TENDER/
            ...

Run:
    python main.py --tenders-root /path/to/tenders_root --workers 12

Each TENDER is processed in its own worker process; within a tender, every
bidder subfolder is processed in turn. Output per tender is ONE consolidated
report listing every bidder (report.pdf / report.xlsx / report.docx) in
<tender_folder>/_output/, plus a per-run log file and a row per bidder in
storage/tender_verifier.db.

Backward compatible: if a tender folder has PDFs sitting directly inside it
(no bidder subfolders — the older single-submission layout), it's treated
as a single bidder named after the tender folder itself.
"""

from __future__ import annotations

import argparse
import glob
import multiprocessing
import os
import sys
import time
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
from core.pdf_reader import iter_pdf_pages, get_page_count
from core.rule_engine import RuleEngine
from reports.report_generator import generate_consolidated_reports
from storage.db import init_db, save_tender_results


def build_groq_pool() -> GroqKeyPool | None:
    """Returns None (LLM fallback disabled, rules/regex-only mode) if no keys are configured —
    the pipeline must run without Groq at all, just with reduced recall on messy fields."""
    try:
        return GroqKeyPool()
    except ValueError:
        return None


def _make_logger(log_lines: list[str]):
    """Every message goes to the console immediately (so you can watch it live)
    AND is buffered for a run_log.txt written into _output/ afterwards, so
    nothing is lost once the terminal scrolls past it."""
    def log(msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log_lines.append(line)
    return log


def process_one_bidder(bidder_folder: str, bidder_name: str, classifier: Classifier,
                        extractor: Extractor, rule_engine: RuleEngine, log) -> tuple[list[dict], object]:
    """Processes every PDF in one bidder's folder. Returns (clean_extracted_docs, verdict)."""
    pdf_files = sorted(glob.glob(os.path.join(bidder_folder, "*.pdf")))
    log(f"    {len(pdf_files)} PDF file(s) found for '{bidder_name}'")

    extracted_docs = []
    for pdf_path in pdf_files:
        source_file = Path(pdf_path).name
        try:
            page_count = get_page_count(pdf_path)
            log(f"    Processing '{source_file}' ({page_count} pages)...")
            for page in iter_pdf_pages(pdf_path):
                classification = classifier.classify_with_llm_fallback(page.text)
                if classification.doc_type is None:
                    log(f"      Page {page.page_number}/{page_count}: unclassified (ocr_source={page.source})")
                    continue

                extraction = extractor.extract(page.text, classification.doc_type, page.page_number)
                log(f"      Page {page.page_number}/{page_count}: {classification.label} "
                    f"(method={classification.method}, score={classification.score})")

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
        except Exception as e:  # noqa: BLE001 — one bad PDF must not kill the whole bidder/tender
            log(f"    ERROR processing '{source_file}': {e}")
            extracted_docs.append({
                "doc_type": "ERROR", "label": "Processing Error", "source_file": source_file,
                "page_number": 0, "fields": {"error": str(e)}, "extraction_notes": {},
            })

    clean_docs = [d for d in extracted_docs if d["doc_type"] != "ERROR"]
    verdict = rule_engine.evaluate(clean_docs, bidder_name=bidder_name) if rule_engine else None
    result_str = verdict.overall_status if verdict else "NOT_EVALUATED"
    log(f"    -> '{bidder_name}': {len(clean_docs)} document(s) found, result = {result_str}")
    return clean_docs, verdict


def process_one_tender(tender_folder: str) -> dict:
    """
    Runs in a worker process. Discovers bidder subfolders, processes each one,
    then writes ONE consolidated report (pdf/xlsx/docx) for the whole tender.
    """
    tender_id = Path(tender_folder).name
    t0 = time.time()
    log_lines: list[str] = []
    log = _make_logger(log_lines)

    log(f"=== TENDER '{tender_id}' starting ===")
    groq_pool = build_groq_pool()
    log(f"Groq LLM fallback: {'ENABLED (' + str(len(groq_pool._pool)) + ' key(s))' if groq_pool else 'DISABLED (rules/regex-only mode)'}")
    classifier = Classifier(groq_pool=groq_pool)
    extractor = Extractor(groq_pool=groq_pool)

    rules_path = os.path.join(tender_folder, "eligibility_rules.json")
    if not os.path.exists(rules_path):
        rules_path = str(CONFIG_DIR / "eligibility_rules_template.json")
        log("No eligibility_rules.json in this tender folder — using the generic template "
            "(PASS/FAIL will use estimated_cost_inr=0, so most value-based criteria will show FAIL/NOT_FOUND "
            "until you add a real eligibility_rules.json here).")
    rule_engine = RuleEngine(rules_path)

    # Discover bidder subfolders (excluding our own _output folder from a previous run).
    bidder_folders = sorted(
        p for p in glob.glob(os.path.join(tender_folder, "*"))
        if os.path.isdir(p) and Path(p).name != "_output"
    )

    if not bidder_folders:
        # Backward compatible fallback: PDFs directly in the tender folder, no bidder subfolders.
        direct_pdfs = glob.glob(os.path.join(tender_folder, "*.pdf"))
        if direct_pdfs:
            log("No bidder subfolders found; PDFs sit directly in the tender folder — "
                "treating the whole tender folder as a single bidder.")
            bidder_folders = [tender_folder]
        else:
            return {"tender_id": tender_id, "status": "ERROR",
                    "detail": "No bidder subfolders and no PDF files found in this tender folder."}

    log(f"Found {len(bidder_folders)} bidder folder(s).")

    bidder_results = []
    for i, bidder_folder in enumerate(bidder_folders, start=1):
        bidder_name = Path(bidder_folder).name
        log(f"[{i}/{len(bidder_folders)}] Bidder: {bidder_name}")
        docs, verdict = process_one_bidder(bidder_folder, bidder_name, classifier, extractor, rule_engine, log)
        bidder_results.append({"bidder_name": bidder_name, "extracted_docs": docs, "verdict": verdict})

        # Each bidder gets its own row in the DB, keyed as "TENDER::BIDDER" so
        # storage/db.py's search_documents() can filter across all tenders/bidders
        # without needing a schema change.
        save_tender_results(
            tender_id=f"{tender_id}::{bidder_name}",
            source_folder=bidder_folder,
            processed_at=datetime.now().isoformat(),
            extracted_docs=docs,
            verdict=verdict,
        )

    output_dir = os.path.join(tender_folder, "_output")
    os.makedirs(output_dir, exist_ok=True)
    log("Writing consolidated report.pdf / report.xlsx / report.docx ...")
    paths = generate_consolidated_reports(tender_id, bidder_results, output_dir)

    elapsed = time.time() - t0
    qualified = sum(1 for b in bidder_results if b["verdict"] and b["verdict"].overall_status == "PASS")
    disqualified = len(bidder_results) - qualified
    log(f"=== TENDER '{tender_id}' done in {elapsed:.1f}s: "
        f"{len(bidder_results)} bidder(s), {qualified} qualified, {disqualified} disqualified ===")

    with open(os.path.join(output_dir, "run_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))

    return {
        "tender_id": tender_id, "status": "OK", "bidders_processed": len(bidder_results),
        "qualified": qualified, "disqualified": disqualified, "elapsed_seconds": round(elapsed, 1),
        "reports": paths,
    }


def main():
    parser = argparse.ArgumentParser(description="Batch tender document verification pipeline (multi-bidder).")
    parser.add_argument("--tenders-root", required=True, help="Folder containing one subfolder per TENDER (each tender containing bidder subfolders).")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()

    init_db()

    tender_folders = sorted(
        p for p in glob.glob(os.path.join(args.tenders_root, "*")) if os.path.isdir(p)
    )
    print(f"Found {len(tender_folders)} tender folder(s). Processing with {args.workers} worker(s)...\n")

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

            if result["status"] == "OK":
                print(f"\n>>> [{len(results)}/{len(tender_folders)}] TENDER '{result['tender_id']}' COMPLETE: "
                      f"{result['bidders_processed']} bidders, {result['qualified']} qualified, "
                      f"{result['disqualified']} disqualified, {result['elapsed_seconds']}s\n")
            else:
                print(f"\n>>> [{len(results)}/{len(tender_folders)}] TENDER '{result['tender_id']}' ERROR:")
                print(result.get("detail", "(no further detail)"))

    ok = sum(1 for r in results if r["status"] == "OK")
    total_bidders = sum(r.get("bidders_processed", 0) for r in results if r["status"] == "OK")
    print(f"\nDone. {ok}/{len(tender_folders)} tender(s) processed successfully ({total_bidders} bidder(s) total).")
    print("Per-tender consolidated reports: <tender_folder>/_output/report.pdf, report.xlsx, report.docx")
    print("Per-tender run log: <tender_folder>/_output/run_log.txt")
    print("All structured data also saved to storage/tender_verifier.db (queryable via storage/db.py).")


if __name__ == "__main__":
    multiprocessing.freeze_support()  # required for ProcessPoolExecutor to work inside a PyInstaller .exe
    main()
