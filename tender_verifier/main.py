"""
Entry point. Point this at a folder of TENDER folders, where each tender
folder contains one BIDDER subfolder per company that submitted a bid:

    tenders_root/
        UPKEEP/                                  <- one tender
            eligibility_rules.json                (optional, see README)
            1.AARUSHI SECURITY INFRA/              <- one bidder
                *.pdf                              (however many files that bidder submitted)
            2.AASTHA TECHNOLOGY SERVICES/
            ...
        ANOTHER_TENDER/
            ...

Run:
    python main.py --tenders-root /path/to/tenders_root --workers 8

=== WHY THIS FILE CHANGED (read this if you're wondering "why was it so slow") ===

The previous version put one whole TENDER in one worker process. That's
fine if you have many small tenders. It is a disaster for the *actual* data
shape here: ONE tender folder (UPKEEP) containing 103 bidder folders. With
--workers 4 and 1 tender folder, only ONE worker process ever had anything
to do — every one of the 103 bidders x ~10 PDFs x ~70 pages ran on a single
CPU core, one page at a time, waiting for each Groq network call to return
before starting the next page. That's the entire reason it looked "stuck".

This version parallelizes at the BIDDER level instead, across ALL tenders
at once. 103 bidders now actually means 103 independent units of work
handed out across --workers processes. On top of that, within a single
bidder:
  - OCR pages (the slow ones) run concurrently in a small thread pool
    (see core/pdf_reader.read_pdf_pages_concurrent), instead of one at a
    time.
  - Groq LLM calls (classification fallback + LLM-extracted fields) run
    concurrently in a thread pool too, since these are network waits, not
    CPU work — the old code blocked on each one serially, which is why
    even PAGES WITH PERFECT NATIVE TEXT (no OCR at all) were taking 5+
    seconds each: that was 100% Groq round-trip latency, one page at a
    time.

Net effect: wall-clock time now scales down with --workers (process-level,
across bidders) MULTIPLIED BY --llm-workers / --ocr-workers (thread-level,
within one bidder), instead of being flat no matter what you set --workers
to.

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
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # reads .env from the current working directory (or wherever it's found up the tree)

sys.path.insert(0, str(Path(__file__).parent))  # allow `core.x` imports when run as a script

from core.classifier import Classifier
from core.extractor import Extractor
from core.groq_client import GroqKeyPool
from core.paths import CONFIG_DIR
from core.pdf_reader import read_pdf_pages_concurrent, get_page_count
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


def _make_logger(log_lines: list[str], tag: str):
    """Every message goes to the console immediately (so you can watch it live)
    AND is buffered for a run_log.txt written into _output/ afterwards, so
    nothing is lost once the terminal scrolls past it. `tag` prefixes every
    line with which bidder it's from, since bidders now run in parallel and
    their log lines interleave in the terminal."""
    def log(msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] [{tag}] {msg}"
        print(line, flush=True)
        log_lines.append(line)
    return log


# ---------------------------------------------------------------------------
# Per-bidder worker (runs inside a ProcessPoolExecutor worker process)
# ---------------------------------------------------------------------------

def process_one_bidder_job(job: dict) -> dict:
    """
    One unit of work for the process pool: read + classify + extract every
    page of every PDF for ONE bidder, then evaluate eligibility.
    """
    tender_id = job["tender_id"]
    bidder_name = job["bidder_name"]
    bidder_folder = job["bidder_folder"]
    ocr_workers = job["ocr_workers"]
    llm_workers = job["llm_workers"]

    log_lines: list[str] = []
    log = _make_logger(log_lines, f"{tender_id}/{bidder_name}")
    t_bidder0 = time.time()

    groq_pool = build_groq_pool()
    classifier = Classifier(groq_pool=groq_pool)
    extractor = Extractor(groq_pool=groq_pool)
    rule_engine = RuleEngine(job["rules_path"])

    pdf_files = sorted(glob.glob(os.path.join(bidder_folder, "*.pdf")))
    log(f"{len(pdf_files)} PDF file(s) found")

    # ---- Stage 1: read every page (OCR pages run concurrently per PDF), ----
    # ---- and do the cheap rule-based classification pass (no network). ----
    page_records = []
    t_read0 = time.time()
    for pdf_path in pdf_files:
        source_file = Path(pdf_path).name
        try:
            page_count = get_page_count(pdf_path)
            log(f"  Reading '{source_file}' ({page_count} pages, OCR concurrency x{ocr_workers})...")
            t_file0 = time.time()
            pages = read_pdf_pages_concurrent(pdf_path, ocr_workers=ocr_workers)
            n_ocr = sum(1 for p in pages if p.source == "ocr")
            log(f"    '{source_file}' read in {time.time() - t_file0:.1f}s "
                f"({n_ocr} OCR page(s), {len(pages) - n_ocr} native)")
            for page in pages:
                page_records.append({
                    "source_file": source_file,
                    "page_count": page_count,
                    "page": page,
                    "classification": classifier.classify_rule_based(page.text),
                })
        except Exception as e:  # noqa: BLE001 — one bad PDF must not kill the whole bidder
            log(f"  ERROR reading '{source_file}': {e}")
            page_records.append({"source_file": source_file, "page_count": 0, "page": None,
                                  "classification": None, "read_error": str(e)})

    n_pages = sum(1 for r in page_records if r["page"] is not None)
    read_elapsed = time.time() - t_read0
    rate = (n_pages / read_elapsed) if read_elapsed > 0 else 0.0
    log(f"Read {n_pages} page(s) across {len(pdf_files)} file(s) in {read_elapsed:.1f}s ({rate:.2f} pages/sec)")

    # ---- Stage 2: LLM classification fallback for pages the rules missed, ----
    # ---- dispatched CONCURRENTLY (these are network calls, not CPU work). ----
    needs_llm_classify = [r for r in page_records
                           if r["page"] is not None and r["classification"].doc_type is None]
    if needs_llm_classify and classifier.groq_pool is not None:
        log(f"{len(needs_llm_classify)} page(s) need LLM classification fallback "
            f"-> running with {llm_workers} concurrent call(s)")
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=llm_workers) as pool:
            futures = {pool.submit(classifier.classify_with_llm_fallback, r["page"].text): r
                       for r in needs_llm_classify}
            for future in as_completed(futures):
                r = futures[future]
                r["classification"] = future.result()
        log(f"LLM classification fallback finished in {time.time() - t0:.1f}s")

    # ---- Stage 3: extract fields for every classified page, LLM-field ----
    # ---- extraction dispatched concurrently for the same reason. ----
    classified = [r for r in page_records
                  if r["page"] is not None and r["classification"].doc_type is not None]
    log(f"{len(classified)}/{n_pages} page(s) classified -> extracting fields "
        f"({llm_workers} concurrent for LLM-required fields)")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=llm_workers) as pool:
        futures = {
            pool.submit(extractor.extract, r["page"].text, r["classification"].doc_type, r["page"].page_number): r
            for r in classified
        }
        for future in as_completed(futures):
            r = futures[future]
            r["extraction"] = future.result()
    log(f"Extraction finished in {time.time() - t0:.1f}s")

    # ---- Stage 4: assemble results in page order (readability), log every page ----
    extracted_docs = []
    ordered = sorted(
        page_records,
        key=lambda r: (r["source_file"], r["page"].page_number if r["page"] else 0),
    )
    for r in ordered:
        if r["page"] is None:
            extracted_docs.append({
                "doc_type": "ERROR", "label": "Processing Error", "source_file": r["source_file"],
                "page_number": 0, "fields": {"error": r.get("read_error", "unknown")}, "extraction_notes": {},
            })
            continue

        page = r["page"]
        classification = r["classification"]
        if classification.doc_type is None:
            log(f"  {r['source_file']} Page {page.page_number}/{r['page_count']}: "
                f"unclassified (ocr_source={page.source})")
            continue

        extraction = r["extraction"]
        log(f"  {r['source_file']} Page {page.page_number}/{r['page_count']}: {classification.label} "
            f"(method={classification.method}, score={classification.score})")
        extracted_docs.append({
            "doc_type": classification.doc_type,
            "label": classification.label,
            "source_file": r["source_file"],
            "page_number": page.page_number,
            "classification_method": classification.method,
            "classification_score": classification.score,
            "ocr_source": page.source,
            "ocr_confidence": page.ocr_confidence,
            "fields": extraction.fields,
            "extraction_notes": extraction.extraction_notes,
        })

    clean_docs = [d for d in extracted_docs if d["doc_type"] != "ERROR"]
    verdict = rule_engine.evaluate(clean_docs, bidder_name=bidder_name) if rule_engine else None
    result_str = verdict.overall_status if verdict else "NOT_EVALUATED"
    elapsed = time.time() - t_bidder0
    log(f"DONE: {len(clean_docs)} document(s) found, result = {result_str}, "
        f"{n_pages} page(s) in {elapsed:.1f}s ({(n_pages / elapsed if elapsed > 0 else 0):.2f} pages/sec overall)")

    save_tender_results(
        tender_id=f"{tender_id}::{bidder_name}",
        source_folder=bidder_folder,
        processed_at=datetime.now().isoformat(),
        extracted_docs=clean_docs,
        verdict=verdict,
    )

    return {
        "tender_id": tender_id,
        "bidder_name": bidder_name,
        "extracted_docs": clean_docs,
        "verdict": verdict,
        "elapsed_seconds": round(elapsed, 1),
        "n_pages": n_pages,
        "log_lines": log_lines,
    }


# ---------------------------------------------------------------------------
# Orchestration: discover jobs across ALL tenders, run them as one flat pool
# ---------------------------------------------------------------------------

def discover_jobs(tenders_root: str, ocr_workers: int, llm_workers: int) -> tuple[list[dict], dict[str, str]]:
    """Returns (jobs, tender_folders_by_id). One job per BIDDER, across every tender."""
    tender_folders = sorted(p for p in glob.glob(os.path.join(tenders_root, "*")) if os.path.isdir(p))
    jobs = []
    tender_folders_by_id: dict[str, str] = {}

    for tender_folder in tender_folders:
        tender_id = Path(tender_folder).name
        tender_folders_by_id[tender_id] = tender_folder

        rules_path = os.path.join(tender_folder, "eligibility_rules.json")
        if not os.path.exists(rules_path):
            rules_path = str(CONFIG_DIR / "eligibility_rules_template.json")

        bidder_folders = sorted(
            p for p in glob.glob(os.path.join(tender_folder, "*"))
            if os.path.isdir(p) and Path(p).name != "_output"
        )

        if not bidder_folders:
            direct_pdfs = glob.glob(os.path.join(tender_folder, "*.pdf"))
            if direct_pdfs:
                bidder_folders = [tender_folder]  # backward-compat: flat single-bidder tender

        for bidder_folder in bidder_folders:
            jobs.append({
                "tender_id": tender_id,
                "tender_folder": tender_folder,
                "bidder_folder": bidder_folder,
                "bidder_name": Path(bidder_folder).name,
                "rules_path": rules_path,
                "ocr_workers": ocr_workers,
                "llm_workers": llm_workers,
            })

    return jobs, tender_folders_by_id


def main():
    parser = argparse.ArgumentParser(description="Batch tender document verification pipeline (multi-bidder, parallel-by-bidder).")
    parser.add_argument("--tenders-root", required=True,
                         help="Folder containing one subfolder per TENDER (each tender containing bidder subfolders).")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4,
                         help="Process-level parallelism, ACROSS BIDDERS (not tenders). "
                              "Set close to your CPU core count — this is the main speed lever.")
    parser.add_argument("--ocr-workers", type=int, default=4,
                         help="Thread-level parallelism for OCR pages WITHIN one bidder.")
    parser.add_argument("--llm-workers", type=int, default=8,
                         help="Thread-level parallelism for Groq calls WITHIN one bidder. "
                              "Raise this if you have several Groq keys configured (more keys = more "
                              "concurrent requests without rate-limiting each other) and your network "
                              "isn't the bottleneck.")
    args = parser.parse_args()

    init_db()

    jobs, tender_folders_by_id = discover_jobs(args.tenders_root, args.ocr_workers, args.llm_workers)
    if not jobs:
        print(f"No bidder folders (or flat-PDF tenders) found under {args.tenders_root}. Nothing to do.")
        return

    n_tenders = len(tender_folders_by_id)
    print(f"Found {n_tenders} tender(s), {len(jobs)} bidder(s) total. "
          f"Processing with {args.workers} process(es) x {args.ocr_workers} OCR thread(s) "
          f"x {args.llm_workers} LLM thread(s) per bidder.\n")

    t0 = time.time()
    results_by_tender: dict[str, list[dict]] = {tid: [] for tid in tender_folders_by_id}
    done_count = 0

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_one_bidder_job, job): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            done_count += 1
            elapsed_so_far = time.time() - t0
            rate = done_count / elapsed_so_far if elapsed_so_far > 0 else 0
            eta = (len(jobs) - done_count) / rate if rate > 0 else float("inf")

            try:
                result = future.result()
                results_by_tender[result["tender_id"]].append(result)
                print(f">>> [{done_count}/{len(jobs)}] {result['tender_id']}/{result['bidder_name']} "
                      f"done in {result['elapsed_seconds']}s ({result['n_pages']} pages) | "
                      f"overall: {rate:.2f} bidders/sec, ETA {eta/60:.1f} min\n")
            except Exception:  # noqa: BLE001
                print(f">>> [{done_count}/{len(jobs)}] {job['tender_id']}/{job['bidder_name']} ERROR:")
                print(traceback.format_exc())
                results_by_tender[job["tender_id"]].append({
                    "tender_id": job["tender_id"], "bidder_name": job["bidder_name"],
                    "extracted_docs": [], "verdict": None, "elapsed_seconds": 0, "n_pages": 0,
                    "log_lines": [f"ERROR: {traceback.format_exc()}"],
                })

    # Now that every bidder in each tender is done, write ONE consolidated report per tender.
    print("\nAll bidders processed. Writing consolidated reports per tender...\n")
    for tender_id, bidder_results in results_by_tender.items():
        tender_folder = tender_folders_by_id[tender_id]
        output_dir = os.path.join(tender_folder, "_output")
        os.makedirs(output_dir, exist_ok=True)

        bidder_results_sorted = sorted(bidder_results, key=lambda r: r["bidder_name"])
        report_input = [{"bidder_name": r["bidder_name"], "extracted_docs": r["extracted_docs"], "verdict": r["verdict"]}
                         for r in bidder_results_sorted]
        paths = generate_consolidated_reports(tender_id, report_input, output_dir)

        all_log_lines = [line for r in bidder_results_sorted for line in r["log_lines"]]
        with open(os.path.join(output_dir, "run_log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(all_log_lines))

        qualified = sum(1 for r in bidder_results_sorted if r["verdict"] and r["verdict"].overall_status == "PASS")
        print(f"TENDER '{tender_id}': {len(bidder_results_sorted)} bidder(s), {qualified} qualified, "
              f"{len(bidder_results_sorted) - qualified} disqualified -> {output_dir}")

    total_elapsed = time.time() - t0
    print(f"\nDone. {len(jobs)} bidder(s) across {n_tenders} tender(s) in {total_elapsed/60:.1f} min "
          f"({len(jobs)/total_elapsed:.2f} bidders/sec).")
    print("Per-tender consolidated reports: <tender_folder>/_output/report.pdf, report.xlsx, report.docx")
    print("Per-tender run log: <tender_folder>/_output/run_log.txt")
    print("All structured data also saved to storage/tender_verifier.db (queryable via storage/db.py).")


if __name__ == "__main__":
    multiprocessing.freeze_support()  # required for ProcessPoolExecutor to work inside a PyInstaller .exe
    main()
