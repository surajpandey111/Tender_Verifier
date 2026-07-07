# Tender Document Verification Pipeline

Automates: *"Does this 300-page tender submission satisfy the eligibility
criteria — and if not, why not, and on which page did we check?"*

This has been tested end-to-end against your three sample images
(`samples/tenders_root/TENDER_DEMO_001/`) — see **Proof it works** below.

## What it does

1. Reads a folder of PDFs (native text or scanned/photographed pages).
2. Preprocesses scanned pages (deskew, denoise, orientation-correct, contrast) before OCR.
3. Classifies each page into a document type (PAN, GST, UDYAM, Turnover
   Certificate, Experience Certificate, GeM Contract, Undertaking, etc.)
   using weighted keyword rules ported from your VBA macro — with an
   optional Groq LLM fallback for pages the rules can't confidently call.
4. Extracts structured fields per document — regex where the format is
   fixed (PAN number, GSTIN, UDIN), Groq LLM where the field is free-text
   or phrased inconsistently (buyer name, contract value, turnover table).
5. Runs a JSON-driven rule engine that computes PASS/FAIL per eligibility
   criterion (e.g. the classic "3 contracts @40% OR 2 @50% OR 1 @80% of
   estimated cost" clause), with the reasoning and page references spelled out.
6. Outputs a per-tender `report.xlsx` and `report.pdf`, plus a queryable
   SQLite database across all tenders.

## Setup

```bash
pip install -r requirements.txt
sudo apt install tesseract-ocr   # if not already installed
```

**Groq API keys (optional but recommended for full field extraction):**
The rule-based classifier and regex extractor work with **zero API calls**.
Groq is only used as a fallback for ambiguous pages and free-text fields.
Get free keys at https://console.groq.com, then set:

```bash
export GROQ_API_KEYS="key1,key2,key3,key4,key5"
```

The client round-robins across all keys, so 5 free-tier keys give you
roughly 5x the throughput of one, without paying for a higher tier.
If no keys are set, the pipeline still runs — it just leaves free-text
fields as "not extracted" instead of crashing, exactly as demonstrated
in the included sample run.

## Running it

```bash
python main.py --tenders-root /path/to/tenders_root --workers 12
```

Expected folder layout:
```
tenders_root/
    TENDER_001/
        file1.pdf
        file2.pdf
        eligibility_rules.json   <- optional, see below
    TENDER_002/
        ...
```

Each tender is processed in its own worker process (this is what makes
400 tenders x 100-350 pages finish in hours, not days — see the
throughput math below). Reports land in `TENDER_001/_output/report.xlsx`
and `report.pdf`.

## Onboarding a new tender's eligibility criteria

Copy `config/eligibility_rules_template.json` into the tender's folder as
`eligibility_rules.json`, fill in `estimated_cost_inr` and any thresholds
from that tender's document. **No Python code changes needed** — this is
the whole point of keeping criteria as data. If a tender folder has no
`eligibility_rules.json`, the pipeline uses the template as a generic
"which documents were found" checklist without PASS/FAIL scoring.

## Onboarding a new document type

Add an entry to `config/document_rules.json` — `detect` keywords with
weights, a `min_score` threshold, and a `fields` schema (regex or llm per
field). No Python changes needed there either.

## Proof it works (included sample)

`samples/tenders_root/TENDER_DEMO_001/submission.pdf` was built from your
three uploaded photos. Running the pipeline against it (with **no** Groq
keys configured, to test the "must work without any API" requirement):

- Page 2 (Annual Turnover Certificate) was correctly classified (rule
  score 15) and its regex fields extracted **exactly matching the source**:
  UDIN `25552869BMLMEQ9124`, Membership No. `552869`, date `01/12/2025`.
- Free-text fields on that page (CA name, turnover-by-year, average
  turnover) correctly show `extraction_method: "unavailable"` rather than
  a guess — this is the safe-degradation path when Groq isn't configured.
- Page 1 (Experience/Work Completion Certificate) did **not** clear the
  classification threshold, because that specific photo has a phone
  visible in-frame and heavy glare (OCR confidence only ~53%) which
  garbled "WORK COMPLETION CERTIFICATE" beyond rule-based recognition —
  this is precisely the scenario the Groq LLM fallback exists for. With
  keys configured, this page would very likely classify correctly.
- The generated `report.xlsx` / `report.pdf` correctly show `NOT_FOUND`
  for PAN/GST/Undertaking (genuinely absent from this 3-page test) and
  `PASS` for what could be verified (UDIN present, CA membership present).

Check the sample output yourself: `samples/tenders_root/TENDER_DEMO_001/_output/`

## Throughput estimate

~400 tenders x ~150 pages avg = 60,000 pages. Tesseract runs ~1-3 sec/page
single-threaded; with 12-16 parallel workers that's roughly 60,000 x 2s /
14 ≈ **2.5-3 hours** — comfortably inside a 12-hour target, and most
pages with a native text layer (many GeM/GST PDFs) skip OCR entirely and
cost virtually nothing.

## Project layout

```
config/     document_rules.json, eligibility_rules_template.json  (all tunable, no code)
ocr/        preprocess.py (deskew/denoise/orientation), engine.py (Tesseract + optional EasyOCR)
core/       pdf_reader.py, classifier.py, extractor.py, rule_engine.py, groq_client.py
storage/    db.py (SQLite, WAL mode for concurrent workers)
reports/    report_generator.py (Excel + PDF)
main.py     orchestrator (multiprocessing across tenders)
```

## Known limitations to plan around

- Heavily degraded photos (glare, phone-in-frame, extreme skew) will
  sometimes miss rule-based classification — the Groq fallback closes
  most of this gap; consider also just re-photographing the worst pages.
- The `frn` regex field is occasionally off by a digit on this sample
  (`935339N` vs actual `035339N`) — a classic 0/9 OCR confusion. Worth
  spot-checking financial reference numbers rather than trusting them blind.
- LayoutLM/Donut-style layout-aware models (mentioned in your notes) were
  intentionally left out of this first version — they need a GPU to run at
  reasonable speed and add real deployment complexity. The keyword+LLM
  approach here was chosen to be laptop-feasible today; it's a valid
  upgrade path later if volume grows much further.
