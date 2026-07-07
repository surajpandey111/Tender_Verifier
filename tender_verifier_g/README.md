# Tender Document Verification Pipeline

Automates the manual Technical Compliance Report (TCR) process: for one
tender with 100+ bidders, each submitting ~10 PDFs (~70 pages), produces
ONE consolidated report listing every bidder's GST/PAN/Udyam/Turnover/
Experience findings, page references, and a plain-English PASS/FAIL
conclusion — in PDF, Excel, and Word.

## Folder structure this expects

```
tenders_root/
    UPKEEP/                                    <- one TENDER
        eligibility_rules.json                  <- optional, see below
        1.AARUSHI SECURITY INFRA/                <- one BIDDER
            *.pdf                                (however many files that bidder submitted)
        2.AASTHA TECHNOLOGY SERVICES/
            *.pdf
        ... (one subfolder per bidder)
    ANOTHER_TENDER/
        ...
```

If your 100+ bidder folders already exist (e.g. downloaded from an
eProcurement portal) but are scattered across different locations, use
`collect_tenders.py` to gather them into this structure in one command —
see that script's docstring for usage.

Backward compatible: a tender folder with PDFs sitting directly inside it
(no bidder subfolders) is treated as a single bidder.

## What it does, per bidder

1. Reads every PDF (native text layer or OCR for scanned/photographed pages).
2. Classifies each page: PAN, GST, UDYAM/MSME, Turnover Certificate,
   Experience Certificate, Technical Specification, Undertaking, etc.
3. Extracts structured fields — regex where the format is fixed (PAN, GSTIN,
   UDIN), Groq LLM where it's free text (buyer name, contract value,
   turnover-by-year table).
4. Evaluates that bidder against the tender's eligibility criteria (JSON-driven,
   no code changes needed per tender) — GST/PAN/Undertaking/Tech-spec
   presence, average annual turnover vs. % of tender value, and the
   "3 contracts @30% OR 2 @50% OR 1 @80%" experience rule.
5. Writes a plain-English conclusion per bidder ("Bidder Qualified" or
   "Bidder Disqualified due to: ...", listing every failing reason) —
   the wording comes from `fail_message` in eligibility_rules.json, so it's
   editable per tender without touching code.

## Setup

```bash
pip install -r requirements.txt
sudo apt install tesseract-ocr   # if not already installed (Windows/Mac: see BUILD_EXE.md)
cp .env.example .env             # then paste your Groq key(s) in .env
python check_setup.py            # verifies every dependency, run this first
```

Rules/regex work with **zero API calls**. Groq is a fallback for ambiguous
pages and free-text fields — the pipeline runs fine without it, with
reduced recall on messy fields.

## Running it

```bash
python main.py --tenders-root tenders_root --workers 8
```

Everything prints live as it runs — which PDF, how many pages, what each
page classified as, and the result per bidder — so you can watch progress
instead of waiting blind. The same output is also saved per tender at
`<tender_folder>/_output/run_log.txt`.

Output per tender, in `<tender_folder>/_output/`:
- `report.pdf` — the numbered A-G format per bidder, matching a manually
  written TCR
- `report.xlsx` — a Summary sheet (one row per bidder) + a Details sheet
  (full text per bidder)
- `report.docx` — same content in Word, for further editing/annotation
- `run_log.txt` — the full verbose log for that tender's run

All structured data is also saved to `storage/tender_verifier.db` (SQLite),
queryable across every tender/bidder — see `storage/db.py`'s
`search_documents()`.

## Onboarding a new tender's eligibility criteria

Copy `config/eligibility_rules_template.json` into the tender's folder as
`eligibility_rules.json`, fill in `estimated_cost_inr` and adjust
percentages/messages to match that tender's actual NIT clause. No Python
changes needed. If a tender folder has no `eligibility_rules.json`, bidders
still get a documents-found report, just without a PASS/FAIL conclusion.

## Onboarding a new document type

Add an entry to `config/document_rules.json` — detection keywords with
weights, a `min_score` threshold, and a `fields` schema (regex or llm per
field). No Python changes needed there either.

## Packaging as a standalone .exe

See `BUILD_EXE.md` for the full guide — including what the recipient's PC
needs (and doesn't need), and how to bundle a portable Tesseract so they
need zero manual installs.

## Project layout

```
config/     document_rules.json, eligibility_rules_template.json  (all tunable, no code)
ocr/        preprocess.py (deskew/denoise/orientation), engine.py (Tesseract + optional EasyOCR)
core/       pdf_reader.py, classifier.py, extractor.py, rule_engine.py, groq_client.py, paths.py
storage/    db.py (SQLite, WAL mode for concurrent workers)
reports/    bidder_narrative.py (shared A-G section builder), report_generator.py (PDF/XLSX/DOCX)
main.py     orchestrator (multiprocessing across tenders, verbose per-bidder/per-page logging)
collect_tenders.py   bulk-gathers scattered bidder folders into one tenders_root
check_setup.py        one-command dependency verification
```

## Known limitations to plan around

- Heavily degraded photos (glare, phone-in-frame, extreme skew) can miss
  rule-based classification or produce OCR-ambiguous characters in ID
  fields (UDIN/PAN/GSTIN) — the Groq fallback closes most of this gap;
  official portal scans and born-digital PDFs OCR far more cleanly than
  phone photos.
- The "Declaration submitted but not in the given format" nuance from a
  manual TCR isn't auto-judged — the pipeline reports presence/absence
  with a page reference; format-compliance judgment is still a human
  step for now.
- E) EMD status is derived (exempted if MSE/Udyam registration is found) —
  verify this matches the actual tender's EMD clause, which can vary.
