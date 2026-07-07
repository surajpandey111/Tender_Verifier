"""
Classifies a single page's text into a document type (PAN, GST, UDYAM,
TURNOVER_CERTIFICATE, EXPERIENCE_CERTIFICATE, GEM_CONTRACT, UNDERTAKING, etc.)

Two layers, in order:
  1. Rule-based scoring — a direct, faithful port of the VBA InStr logic
     (TURNOVER_FOUND, GEM_CONTRAT_FOUND, the PAN/GST/UDYAM If-blocks), except
     generalized into a JSON-driven weighted scorer so new document types or
     tuning changes never require touching this file — only document_rules.json.
  2. Groq LLM fallback — invoked ONLY when no rule crosses its min_score
     threshold (i.e. genuinely ambiguous pages). Sends just that one page's
     text, asks for a JSON classification. If Groq is unreachable or returns
     nothing usable, the page is simply marked UNCLASSIFIED — the pipeline
     never blocks on this.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.groq_client import GroqKeyPool

RULES_PATH = Path(__file__).parent.parent / "config" / "document_rules.json"


@dataclass
class ClassificationResult:
    doc_type: Optional[str]           # key into document_rules.json, or None
    label: Optional[str]
    score: int
    method: str                        # "rule" | "llm" | "unclassified"
    all_scores: dict = field(default_factory=dict)  # doc_type -> score, for auditability


def _load_rules() -> dict:
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["document_types"]


class Classifier:
    def __init__(self, groq_pool: Optional[GroqKeyPool] = None, rules: Optional[dict] = None):
        self.rules = rules or _load_rules()
        self.groq_pool = groq_pool  # may be None -> LLM fallback simply disabled

    # ---------- Layer 1: rule-based scoring (ported VBA logic) ----------

    @staticmethod
    def _contains(text: str, phrase: str) -> bool:
        return phrase.lower() in text.lower()

    def _score_doc_type(self, text: str, rule: dict) -> int:
        score = 0
        detect = rule["detect"]

        for item in detect.get("any_of_high", []):
            if self._contains(text, item["phrase"]):
                requires = item.get("requires_with")
                if requires and not all(self._contains(text, r) for r in requires):
                    continue  # e.g. VBA's "INCOME TAX DEPARTMENT + GOVT OF INDIA" combo requirement
                score += item["weight"]

        for item in detect.get("supporting", []):
            if self._contains(text, item["phrase"]):
                score += item["weight"]

        return score

    def classify_rule_based(self, text: str) -> ClassificationResult:
        scores = {}
        for doc_type, rule in self.rules.items():
            scores[doc_type] = self._score_doc_type(text, rule)

        # pick the highest-scoring type that clears ITS OWN min_score
        best_type, best_score = None, -1
        for doc_type, score in scores.items():
            min_required = self.rules[doc_type]["min_score"]
            if score >= min_required and score > best_score:
                best_type, best_score = doc_type, score

        if best_type is None:
            return ClassificationResult(None, None, 0, "unclassified", scores)

        return ClassificationResult(
            doc_type=best_type,
            label=self.rules[best_type]["label"],
            score=best_score,
            method="rule",
            all_scores=scores,
        )

    # ---------- Layer 2: Groq fallback for ambiguous pages ----------

    def classify_with_llm_fallback(self, text: str) -> ClassificationResult:
        rule_result = self.classify_rule_based(text)
        if rule_result.doc_type is not None or self.groq_pool is None:
            return rule_result

        if len(text.strip()) < 15:
            return rule_result  # near-empty page (blank/divider) — not worth an LLM call

        doc_type_list = ", ".join(self.rules.keys())
        system_prompt = (
            "You classify a single scanned tender/procurement document page into ONE of these "
            f"types, or 'NONE' if it doesn't match any: {doc_type_list}. "
            'Respond as JSON: {"doc_type": "<TYPE_OR_NONE>", "confidence": <0-100>}'
        )
        # Truncate defensively — this must always be a single page, never a whole document.
        user_prompt = text[:3000]

        response = self.groq_pool.complete_json(system_prompt, user_prompt, max_tokens=100)
        if not response or response.get("doc_type") in (None, "NONE"):
            return rule_result  # fall back to "unclassified" rather than guessing

        doc_type = response["doc_type"]
        if doc_type not in self.rules:
            return rule_result  # LLM hallucinated a type not in our schema — ignore it

        return ClassificationResult(
            doc_type=doc_type,
            label=self.rules[doc_type]["label"],
            score=int(response.get("confidence", 0)),
            method="llm",
            all_scores=rule_result.all_scores,
        )
