"""
The "brain": takes everything extracted from a tender's PDFs (one record per
matched document) plus that tender's eligibility_rules_*.json, and produces a
PASS/FAIL/NOT_FOUND verdict per criterion, with the reasoning spelled out —
this is what turns "Experience Certificate found on page 82" into
"Experience Criteria: PASS (this contract alone covers 92% of the one-contract
80% threshold)".

Nothing here is tender-specific in code. All thresholds, document-type
mappings and percentage rules live in the JSON file. To onboard a new tender,
copy eligibility_rules_template.json, fill in numbers, done.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class CriterionVerdict:
    id: str
    label: str
    status: str          # "PASS" | "FAIL" | "NOT_FOUND"
    reason: str
    evidence: list[dict] = field(default_factory=list)  # [{doc_type, page_number, value}]


@dataclass
class TenderVerdict:
    tender_id: str
    overall_status: str   # "PASS" | "FAIL"
    criteria: list[CriterionVerdict]


def _to_number(value: Any) -> Optional[float]:
    """Best-effort numeric coercion for values that came from OCR/LLM and may
    contain currency symbols, commas, 'Rs', 'Lakhs', etc."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    lakhs = "lakh" in s.lower() or "lac" in s.lower()
    crore = "crore" in s.lower()
    cleaned = re.sub(r"[^\d.]", "", s)
    if not cleaned:
        return None
    num = float(cleaned)
    if lakhs:
        num *= 100_000
    if crore:
        num *= 10_000_000
    return num


class RuleEngine:
    def __init__(self, eligibility_rules_path: str):
        with open(eligibility_rules_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.estimated_cost = self.config.get("estimated_cost_inr") or 0

    def _docs_of_type(self, extracted_docs: list[dict], doc_type: str) -> list[dict]:
        return [d for d in extracted_docs if d["doc_type"] == doc_type]

    def _evaluate_document_present(self, criterion: dict, extracted_docs: list[dict]) -> CriterionVerdict:
        matches = self._docs_of_type(extracted_docs, criterion["source_document_type"])
        if not matches:
            return CriterionVerdict(criterion["id"], criterion["label"], "NOT_FOUND",
                                     f"No {criterion['source_document_type']} document found in submission.")
        d = matches[0]
        return CriterionVerdict(
            criterion["id"], criterion["label"], "PASS",
            f"Found on page {d['page_number']}.",
            evidence=[{"doc_type": d["doc_type"], "page_number": d["page_number"]}],
        )

    def _evaluate_field_present(self, criterion: dict, extracted_docs: list[dict]) -> CriterionVerdict:
        matches = self._docs_of_type(extracted_docs, criterion["source_document_type"])
        for d in matches:
            value = d["fields"].get(criterion["field_used"])
            if value:
                return CriterionVerdict(
                    criterion["id"], criterion["label"], "PASS",
                    f"{criterion['field_used']} = {value} (page {d['page_number']}).",
                    evidence=[{"doc_type": d["doc_type"], "page_number": d["page_number"], "value": value}],
                )
        status = "NOT_FOUND" if not matches else "FAIL"
        return CriterionVerdict(criterion["id"], criterion["label"], status,
                                 f"{criterion['field_used']} not found on any {criterion['source_document_type']} page.")

    def _evaluate_min_value(self, criterion: dict, extracted_docs: list[dict]) -> CriterionVerdict:
        matches = self._docs_of_type(extracted_docs, criterion["source_document_type"])
        if not matches:
            return CriterionVerdict(criterion["id"], criterion["label"], "NOT_FOUND",
                                     f"No {criterion['source_document_type']} found.")

        threshold = criterion.get("min_value_inr") or 0
        if criterion.get("min_value_pct_of_estimate") and self.estimated_cost:
            threshold = self.estimated_cost * criterion["min_value_pct_of_estimate"] / 100


        best_value, best_doc = 0.0, None
        for d in matches:
            val = _to_number(d["fields"].get(criterion["field_used"]))
            if val and val > best_value:
                best_value, best_doc = val, d

        if best_doc is None:
            return CriterionVerdict(criterion["id"], criterion["label"], "FAIL",
                                     f"{criterion['field_used']} could not be read as a number from the submitted document(s).")

        status = "PASS" if best_value >= threshold else "FAIL"
        reason = (f"{criterion['field_used']} = {best_value:,.0f} vs required minimum {threshold:,.0f} "
                  f"(page {best_doc['page_number']}).")
        return CriterionVerdict(criterion["id"], criterion["label"], status, reason,
                                 evidence=[{"doc_type": best_doc["doc_type"], "page_number": best_doc["page_number"],
                                            "value": best_value}])

    def _evaluate_experience_or_count(self, criterion: dict, extracted_docs: list[dict]) -> CriterionVerdict:
        """
        Implements the classic GeM/CPPP clause:
          3 similar contracts >= 40% of estimate, OR
          2 similar contracts >= 50% of estimate, OR
          1 similar contract  >= 80% of estimate.
        aggregation="individual_contract_meets_threshold" means each contract counted
        toward an option must itself individually clear that option's per-contract %.
        """
        matches = self._docs_of_type(extracted_docs, criterion["source_document_type"])
        if not matches:
            return CriterionVerdict(criterion["id"], criterion["label"], "NOT_FOUND",
                                     "No Experience/Work Completion Certificates found.")

        values = []
        for d in matches:
            val = _to_number(d["fields"].get(criterion["field_used"]))
            if val:
                values.append((val, d))
        values.sort(key=lambda x: x[0], reverse=True)

        if not self.estimated_cost:
            return CriterionVerdict(criterion["id"], criterion["label"], "FAIL",
                                     "estimated_cost_inr is not set in this tender's eligibility rules file — cannot compute percentage thresholds.")

        for option in criterion["options"]:
            need_count = option["min_count"]
            need_pct = option["min_value_pct_of_estimate"]
            per_contract_threshold = self.estimated_cost * need_pct / 100
            qualifying = [(v, d) for v, d in values if v >= per_contract_threshold]
            if len(qualifying) >= need_count:
                evidence = [{"doc_type": d["doc_type"], "page_number": d["page_number"], "value": v}
                            for v, d in qualifying[:need_count]]
                reason = (f"{len(qualifying)} contract(s) individually meet >= {need_pct}% of estimated cost "
                          f"(₹{per_contract_threshold:,.0f}); only {need_count} required for this option.")
                return CriterionVerdict(criterion["id"], criterion["label"], "PASS", reason, evidence)

        best_option = criterion["options"][-1]
        return CriterionVerdict(
            criterion["id"], criterion["label"], "FAIL",
            f"No option satisfied. Submitted contracts (₹): {[f'{v:,.0f}' for v, _ in values]}. "
            f"Estimated cost: ₹{self.estimated_cost:,.0f}. None of the 3/2/1-contract "
            f"(40%/50%/80%) options were met.",
        )

    def evaluate(self, extracted_docs: list[dict]) -> TenderVerdict:
        evaluators = {
            "document_present": self._evaluate_document_present,
            "field_present": self._evaluate_field_present,
            "min_value": self._evaluate_min_value,
            "experience_or_count": self._evaluate_experience_or_count,
        }
        verdicts = []
        for criterion in self.config["criteria"]:
            fn = evaluators.get(criterion["type"])
            if not fn:
                verdicts.append(CriterionVerdict(criterion["id"], criterion["label"], "FAIL",
                                                  f"Unknown criterion type '{criterion['type']}' in config."))
                continue
            verdicts.append(fn(criterion, extracted_docs))

        overall = "PASS" if all(v.status == "PASS" for v in verdicts) else "FAIL"
        return TenderVerdict(tender_id=self.config["tender_id"], overall_status=overall, criteria=verdicts)
