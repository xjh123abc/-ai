# Keep voucher drafting separate from ERP posting

Status: Accepted

Voucher drafting owns policy-based suggestions, immutable versions, and accounting validation. Posting owns authorization checks, idempotent attempts, connector calls, and ERP read-back. Only Posting may cause an ERP write; suggestions and approvals remain local records. This split preserves ERP as the accounting system of record and makes the no-authorization export path explicit without pretending a draft is posted.
