"""
Extracts structured fields from a page once it's been classified.

Per document_rules.json, each field is one of:
  - "regex": deterministic, free, instant — used for PAN/GSTIN/UDIN/dates/etc.
    which follow a fixed pattern. Zero token cost.
  - "llm": free-text or "same meaning, many wordings" fields (buyer name,
    contract value phrased as "Total Contract value" vs "Contract Value:",
    turnover-by-year tables, etc.) where regex is brittle. These go to Groq,
    but ONLY the current page's text is sent (never the whole PDF), and only
    the fields the schema actually asks for, in a single batched JSON call
    per page — not one call per field.

If Groq is unavailable, "llm" fields are simply left as null with an
"extraction_method": "unavailable" note — the rest of the pipeline (rule
engine, reports) treats missing fields as "not found", which is the same
place a human would end up who couldn't read that field either, so it
degrades sensibly rather than crashing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Optional

from core.groq_client import GroqKeyPool

RULES_PATH = Path(__file__).parent.parent / "config" / "document_rules.json"


def _load_rules() -> dict:
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["document_types"]


@dataclass
class ExtractionResult:
    doc_type: str
    page_number: int
    fields: dict[str, Any] = dc_field(default_factory=dict)
    missing_required: list[str] = dc_field(default_factory=list)
    extraction_notes: dict[str, str] = dc_field(default_factory=dict)  # field -> "regex"|"llm"|"missing"


class Extractor:
    def __init__(self, groq_pool: Optional[GroqKeyPool] = None, rules: Optional[dict] = None):
        self.rules = rules or _load_rules()
        self.groq_pool = groq_pool

    # ---------- regex fields ----------

    @staticmethod
    def _extract_regex(text: str, pattern: str, context: Optional[str] = None) -> Optional[str]:
        if context:
            # search near the context label first (e.g. "Membership No" -> nearby number),
            # falling back to a global search if that fails.
            ctx_idx = text.lower().find(context.lower())
            if ctx_idx != -1:
                window = text[ctx_idx: ctx_idx + len(context) + 40]
                m = re.search(pattern, window)
                if m:
                    return m.group(0)
        m = re.search(pattern, text)
        return m.group(0) if m else None

    # ---------- llm fields (batched, single page) ----------

    def _extract_llm_fields(self, text: str, doc_type: str, llm_field_names: list[str], rule: dict) -> dict:
        if not self.groq_pool or not llm_field_names:
            return {}

        field_descriptions = []
        for fname in llm_field_names:
            shape = rule["fields"][fname].get("shape", "short text")
            field_descriptions.append(f'  - "{fname}": {shape}')

        system_prompt = (
            f"Extract fields from a single page of a '{rule['label']}' document. "
            "If a field is not present on this page, use null. "
            "Respond as a JSON object with exactly these keys:\n" + "\n".join(field_descriptions)
        )
        user_prompt = text[:3000]  # hard cap — this must be page-level, not document-level

        result = self.groq_pool.complete_json(system_prompt, user_prompt, max_tokens=600)
        return result or {}

    # ---------- orchestration ----------

    def extract(self, text: str, doc_type: str, page_number: int) -> ExtractionResult:
        rule = self.rules[doc_type]
        result = ExtractionResult(doc_type=doc_type, page_number=page_number)

        llm_field_names = []
        for fname, fdef in rule["fields"].items():
            ftype = "regex" if "regex" in fdef else fdef.get("type", "llm")

            if ftype == "regex":
                value = self._extract_regex(text, fdef["regex"], fdef.get("context"))
                result.fields[fname] = value
                result.extraction_notes[fname] = "regex" if value else "missing"
            else:
                llm_field_names.append(fname)

        if llm_field_names:
            llm_values = self._extract_llm_fields(text, doc_type, llm_field_names, rule)
            for fname in llm_field_names:
                value = llm_values.get(fname)
                result.fields[fname] = value
                result.extraction_notes[fname] = "llm" if value not in (None, "", "null") else (
                    "unavailable" if not self.groq_pool else "missing"
                )

        # required-field audit — this is what tells the report generator to flag a document as incomplete
        for fname, fdef in rule["fields"].items():
            if fdef.get("required") and not result.fields.get(fname):
                result.missing_required.append(fname)

        return result
