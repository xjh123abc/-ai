"""Document roles supported by incremental business-event collection.

Classification and evidence collection are independent of accounting readiness.
Recognizing a supporting document never authorizes voucher creation.
"""

ROLE_DOCUMENT_TYPES = {
    "INVOICE": "VAT_INVOICE",
    "GOODS_RECEIPT": "GOODS_RECEIPT",
    "BANK_RECEIPT": "BANK_RECEIPT",
    "PAYMENT_PROOF": "PAYMENT_PROOF",
    "CONTRACT": "CONTRACT",
    "ACCEPTANCE_REPORT": "ACCEPTANCE_REPORT",
    "REIMBURSEMENT_FORM": "REIMBURSEMENT_FORM",
    "TAXI_RECEIPT": "TAXI_RECEIPT",
    "TOLL_RECEIPT": "TOLL_RECEIPT",
}

EVENT_DOCUMENT_ROLES = {
    "PURCHASE": frozenset({"INVOICE", "GOODS_RECEIPT", "BANK_RECEIPT", "PAYMENT_PROOF",
                           "CONTRACT", "ACCEPTANCE_REPORT"}),
    "EXPENSE": frozenset({"INVOICE", "BANK_RECEIPT", "PAYMENT_PROOF", "REIMBURSEMENT_FORM",
                          "TAXI_RECEIPT", "TOLL_RECEIPT", "CONTRACT"}),
    "ASSET_PURCHASE": frozenset({"INVOICE", "BANK_RECEIPT", "CONTRACT", "ACCEPTANCE_REPORT"}),
    "RENTAL": frozenset({"INVOICE", "BANK_RECEIPT", "CONTRACT", "ACCEPTANCE_REPORT"}),
}


def role_allowed(event_type: str, role: str) -> bool:
    return role in EVENT_DOCUMENT_ROLES.get(event_type, ())
