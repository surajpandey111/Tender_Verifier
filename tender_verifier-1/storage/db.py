"""
SQLite storage. At ~400 tenders this comfortably avoids needing Postgres,
while still giving structured, queryable storage instead of "keep the OCR
text lying around in files" — e.g. "find every tender where the buyer was
'Station HQ Kalimpong'" becomes a single SQL query across the whole archive.

Schema:
  tenders            — one row per tender folder processed
  documents          — one row per classified page/document found
  extracted_fields   — one row per (document, field_name) — long/narrow, so
                        adding a new field type later needs no migration
  verdicts           — one row per (tender, criterion) eligibility result
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from core.paths import STORAGE_DIR

DB_PATH = STORAGE_DIR / "tender_verifier.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    tender_id       TEXT PRIMARY KEY,
    source_folder   TEXT,
    processed_at    TEXT,
    overall_status  TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_id       TEXT NOT NULL REFERENCES tenders(tender_id),
    source_file     TEXT NOT NULL,
    page_number     INTEGER NOT NULL,
    doc_type        TEXT NOT NULL,
    label           TEXT,
    classification_method TEXT,
    classification_score  INTEGER,
    ocr_source      TEXT,
    ocr_confidence  REAL
);

CREATE TABLE IF NOT EXISTS extracted_fields (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     INTEGER NOT NULL REFERENCES documents(id),
    field_name      TEXT NOT NULL,
    field_value     TEXT,
    extraction_method TEXT
);

CREATE TABLE IF NOT EXISTS verdicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_id       TEXT NOT NULL REFERENCES tenders(tender_id),
    criterion_id    TEXT NOT NULL,
    label           TEXT,
    status          TEXT,
    reason          TEXT,
    evidence_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_tender ON documents(tender_id);
CREATE INDEX IF NOT EXISTS idx_documents_doctype ON documents(doc_type);
CREATE INDEX IF NOT EXISTS idx_fields_document ON extracted_fields(document_id);
CREATE INDEX IF NOT EXISTS idx_verdicts_tender ON verdicts(tender_id);
"""


@contextmanager
def get_connection(db_path: Path = DB_PATH):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")  # allows concurrent worker writes without locking errors
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH):
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)


def save_tender_results(tender_id: str, source_folder: str, processed_at: str,
                         extracted_docs: list[dict], verdict, db_path: Path = DB_PATH):
    """
    extracted_docs: list of dicts with keys doc_type, label, page_number,
                    source_file, classification_method, classification_score,
                    ocr_source, ocr_confidence, fields (dict), extraction_notes (dict)
    verdict: a rule_engine.TenderVerdict
    """
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tenders (tender_id, source_folder, processed_at, overall_status) VALUES (?, ?, ?, ?)",
            (tender_id, source_folder, processed_at, verdict.overall_status),
        )

        for doc in extracted_docs:
            cur = conn.execute(
                """INSERT INTO documents
                   (tender_id, source_file, page_number, doc_type, label,
                    classification_method, classification_score, ocr_source, ocr_confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (tender_id, doc["source_file"], doc["page_number"], doc["doc_type"], doc.get("label"),
                 doc.get("classification_method"), doc.get("classification_score"),
                 doc.get("ocr_source"), doc.get("ocr_confidence")),
            )
            document_id = cur.lastrowid
            for fname, fvalue in doc.get("fields", {}).items():
                method = doc.get("extraction_notes", {}).get(fname)
                conn.execute(
                    "INSERT INTO extracted_fields (document_id, field_name, field_value, extraction_method) VALUES (?, ?, ?, ?)",
                    (document_id, fname, json.dumps(fvalue) if isinstance(fvalue, (list, dict)) else fvalue, method),
                )

        for c in verdict.criteria:
            conn.execute(
                "INSERT INTO verdicts (tender_id, criterion_id, label, status, reason, evidence_json) VALUES (?, ?, ?, ?, ?, ?)",
                (tender_id, c.id, c.label, c.status, c.reason, json.dumps(c.evidence)),
            )


def search_documents(doc_type: str = None, field_name: str = None, field_value_like: str = None,
                      db_path: Path = DB_PATH) -> list[dict]:
    """Example: search_documents(doc_type='EXPERIENCE_CERTIFICATE', field_name='buyer', field_value_like='Kalimpong')"""
    query = """SELECT d.tender_id, d.source_file, d.page_number, d.doc_type, ef.field_name, ef.field_value
               FROM documents d JOIN extracted_fields ef ON ef.document_id = d.id WHERE 1=1"""
    params = []
    if doc_type:
        query += " AND d.doc_type = ?"
        params.append(doc_type)
    if field_name:
        query += " AND ef.field_name = ?"
        params.append(field_name)
    if field_value_like:
        query += " AND ef.field_value LIKE ?"
        params.append(f"%{field_value_like}%")

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
