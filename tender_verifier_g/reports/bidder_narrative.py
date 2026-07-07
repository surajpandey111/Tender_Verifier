"""
Turns one bidder's (extracted_docs, verdict) into the exact lettered
A) Legal Status / B) Declaration / C) Technical Specification / D) MSE /
E) EMD / F) Turnover / G) Experience / CONCLUSION structure from the
manually-written TCR sample this project is replacing — so PDF, XLSX and
DOCX generators all read from one consistent source instead of drifting.
"""

from __future__ import annotations

from typing import Optional


def _find_doc(extracted_docs: list[dict], doc_type: str) -> Optional[dict]:
    matches = [d for d in extracted_docs if d["doc_type"] == doc_type]
    return matches[0] if matches else None


def _all_docs(extracted_docs: list[dict], doc_type: str) -> list[dict]:
    return [d for d in extracted_docs if d["doc_type"] == doc_type]


def _fmt(value, fallback="(not read)") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def build_bidder_sections(bidder_name: str, extracted_docs: list[dict], verdict) -> dict:
    gst = _find_doc(extracted_docs, "GST")
    pan = _find_doc(extracted_docs, "PAN")
    udyam = (_find_doc(extracted_docs, "UDYAM")
             or _find_doc(extracted_docs, "UDYOG_AADHAR")
             or _find_doc(extracted_docs, "MSME"))
    turnover = _find_doc(extracted_docs, "TURNOVER_CERTIFICATE")
    undertaking = _find_doc(extracted_docs, "UNDERTAKING")
    tech_spec = _find_doc(extracted_docs, "TECHNICAL_SPECIFICATION")
    experiences = _all_docs(extracted_docs, "EXPERIENCE_CERTIFICATE")

    # A) Legal Status
    legal_status = []
    if gst:
        legal_status.append(f"Copy of GST:- {_fmt(gst['fields'].get('gstin'))} submitted (page {gst['page_number']}).")
    else:
        legal_status.append("Copy of GST:- NOT SUBMITTED.")
    if pan:
        legal_status.append(f"Copy of PAN:- {_fmt(pan['fields'].get('pan_number'))} submitted (page {pan['page_number']}).")
    else:
        legal_status.append("Copy of PAN:- NOT SUBMITTED.")

    # B) Declaration
    declaration = (f"Submitted (page {undertaking['page_number']})." if undertaking else "NOT SUBMITTED.")

    # C) Technical Specification
    technical_spec = (f"Submitted (page {tech_spec['page_number']})." if tech_spec else "NOT SUBMITTED.")

    # D) MSE / Udyam
    if udyam:
        reg_no = (udyam["fields"].get("udyam_number") or udyam["fields"].get("udyog_aadhar_number")
                  or udyam["fields"].get("registration_number"))
        mse = f"Udyam/MSME registration no. {_fmt(reg_no)} submitted (page {udyam['page_number']})."
    else:
        mse = "NOT SUBMITTED."

    # E) EMD — derived: exempted if MSE/Udyam present, per the sample's own logic
    emd = "Exempted from EMD (registered under MSE/Udyam)." if udyam else "EMD applicable (no MSE/Udyam registration found — verify manually)."

    # F) Turnover
    if turnover:
        avg = _fmt(turnover["fields"].get("average_annual_turnover"))
        udin = _fmt(turnover["fields"].get("udin"))
        turnover_line = f"UDIN: {udin}  Avg Turnover Rs. {avg} (page {turnover['page_number']})."
    else:
        turnover_line = "NOT SUBMITTED."

    # G) Experience — every certificate found, one line each, matching the sample's lettered sub-list
    experience_lines = []
    if experiences:
        for e in experiences:
            f = e["fields"]
            amount = _fmt(f.get("total_contract_value"))
            ref_no = _fmt(f.get("work_order_no") or f.get("contract_no"))
            period_from = _fmt(f.get("period_from") or f.get("commencement_date"))
            period_to = _fmt(f.get("period_to") or f.get("work_extended_upto"))
            company = _fmt(f.get("buyer"))
            experience_lines.append(
                f"Rs {amount} vide {ref_no}, period {period_from} to {period_to}, "
                f"issued by {company} (page {e['page_number']})."
            )
    else:
        experience_lines = ["NOT SUBMITTED."]

    conclusion = verdict.conclusion_text() if verdict else "Not evaluated — no eligibility_rules.json found for this tender (documents-found checklist only)."
    overall_status = verdict.overall_status if verdict else "NOT_EVALUATED"

    return {
        "bidder_name": bidder_name,
        "legal_status": legal_status,
        "declaration": declaration,
        "technical_spec": technical_spec,
        "mse": mse,
        "emd": emd,
        "turnover": turnover_line,
        "experience": experience_lines,
        "conclusion": conclusion,
        "overall_status": overall_status,
        # raw values for the XLSX summary sheet
        "raw": {
            "gstin": gst["fields"].get("gstin") if gst else None,
            "pan_number": pan["fields"].get("pan_number") if pan else None,
            "avg_turnover": turnover["fields"].get("average_annual_turnover") if turnover else None,
            "udin": turnover["fields"].get("udin") if turnover else None,
            "experience_count": len(experiences),
        },
    }
