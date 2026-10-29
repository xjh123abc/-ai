import unittest

from domain.document_matching import match_document_candidate
from domain.document_registry import role_allowed
from ingestion.candidates import document_type_candidate


def confirmed(**fields):
    return {key: {"normalized_value": value} for key, value in fields.items()}


class IncrementalMatchingTests(unittest.TestCase):
    def test_amount_alone_never_creates_a_suggestion(self):
        original = confirmed(gross_amount="4448.00")
        candidate = confirmed(gross_amount="4448.00")
        self.assertIsNone(match_document_candidate([("INVOICE", original)], candidate))

    def test_amount_and_date_recommend_with_explicit_reasons(self):
        original = confirmed(gross_amount="4448.00", application_date="2024-12-07")
        candidate = confirmed(gross_amount="4448.00", payment_date="2024-12-08")
        assessment = match_document_candidate([("REIMBURSEMENT_FORM", original)], candidate)
        self.assertEqual(assessment["score"], 55)
        self.assertEqual(len(assessment["reasons"]), 2)

    def test_distant_date_and_wrong_parties_do_not_suggest(self):
        original = confirmed(gross_amount="100.00", issue_date="2024-01-01",
                             buyer="甲科技有限公司", seller="乙供应商有限公司")
        candidate = confirmed(gross_amount="100.00", settlement_date="2024-08-01",
                              payer="丙科技有限公司", payee="丁供应商有限公司")
        self.assertIsNone(match_document_candidate([("INVOICE", original)], candidate))

    def test_both_transaction_parties_and_date_work_with_split_invoices(self):
        original = confirmed(gross_amount="22600.00", issue_date="2024-12-01",
                             buyer="Buyer Ltd", seller="Seller Ltd")
        candidate = confirmed(gross_amount="45200.00", settlement_date="2024-12-02",
                              payer="Buyer Ltd", payee="Seller Ltd")
        assessment = match_document_candidate([("INVOICE", original)], candidate)
        self.assertEqual(assessment["score"], 60)
        self.assertIn("交易主体一致", assessment["reasons"])

    def test_reimbursement_type_extends_expense_only(self):
        self.assertTrue(role_allowed("EXPENSE", "REIMBURSEMENT_FORM"))
        self.assertTrue(role_allowed("EXPENSE", "TAXI_RECEIPT"))
        self.assertFalse(role_allowed("PURCHASE", "REIMBURSEMENT_FORM"))
        self.assertEqual(document_type_candidate("差旅费报销单 2024年12月7日 4448.00")["normalized_value"],
                         "REIMBURSEMENT_FORM")


if __name__ == "__main__":
    unittest.main()
