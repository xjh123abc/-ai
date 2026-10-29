"""Explainable, read-only suggestions for late evidence in an existing business case.

This intentionally does not make an accounting decision or create a document link.
Only independently confirmed document fields are used as evidence.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation


DATE_KEYS = ("issue_date", "receipt_date", "settlement_date", "payment_date",
             "application_date", "contract_date", "acceptance_date")


def _value(fields: dict, key: str):
    value = fields.get(key)
    return value.get("normalized_value") if isinstance(value, dict) else value


def _amount(fields: dict) -> Decimal | None:
    value = _value(fields, "gross_amount")
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(",", "").replace("，", ""))
        return result if result > 0 and result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _text(fields: dict, key: str) -> str | None:
    value = _value(fields, key)
    if value in (None, ""):
        return None
    normalized = re.sub(r"[\s（）()·•\-.]", "", str(value)).casefold()
    return normalized if len(normalized) >= 3 else None


def _date(fields: dict) -> date | None:
    for key in DATE_KEYS:
        value = _value(fields, key)
        if value:
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                continue
    return None


def match_document_candidate(existing: list[tuple[str, dict]], candidate_fields: dict) -> dict | None:
    """Rank confirmed evidence; two independent signals are needed for a suggestion.

    Amount alone is deliberately insufficient (e.g. recurring rent and travel costs).
    Scores are heuristic rankings, never probabilistic confidence or auto-approval.
    """
    if not existing:
        return None
    reasons: list[str] = []
    score = 0
    signal_count = 0
    candidate_amount = _amount(candidate_fields)
    amounts = [_amount(fields) for _, fields in existing]
    amounts = [value for value in amounts if value is not None]
    if candidate_amount is not None and (candidate_amount in amounts or
            len(amounts) > 1 and candidate_amount == sum(amounts, Decimal("0"))):
        score += 45
        signal_count += 1
        reasons.append("含税金额与当前业务的已确认单据一致")

    # Buyer/payer and seller/payee occupy the same sides of a transaction.
    # An applicant on a reimbursement form can match the payment recipient.
    sources = ({_text(fields, key) for _, fields in existing for key in keys} - {None}
               for keys in (("buyer", "payer"), ("seller", "payee", "applicant")))
    source_payers, source_payees = sources
    candidate_payers = {_text(candidate_fields, key) for key in ("buyer", "payer")} - {None}
    candidate_payees = {_text(candidate_fields, key) for key in ("seller", "payee", "applicant")} - {None}
    matches = int(bool(source_payers & candidate_payers)) + int(bool(source_payees & candidate_payees))
    if matches:
        score += 25 * matches
        signal_count += matches
        reasons.append("交易主体一致" if matches == 2 else "一方交易主体一致")

    candidate_contract = _text(candidate_fields, "contract_number")
    if candidate_contract and any(_text(fields, "contract_number") == candidate_contract for _, fields in existing):
        score += 45
        signal_count += 1
        reasons.append("合同编号一致")

    candidate_date = _date(candidate_fields)
    dates = [_date(fields) for _, fields in existing]
    deltas = [abs((candidate_date - previous).days) for previous in dates
              if candidate_date is not None and previous is not None]
    if deltas and min(deltas) <= 7:
        score += 10
        signal_count += 1
        reasons.append("单据日期相差不超过 7 天")
    elif deltas and min(deltas) <= 30:
        score += 4
        signal_count += 1
        reasons.append("单据日期相差不超过 30 天")

    if signal_count < 2 or score < 55:
        return None
    return {"score": min(score, 100), "reasons": reasons}
