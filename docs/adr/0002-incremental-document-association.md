# ADR-0002: Incremental document association

Status: Implemented (minimum increment)

## Context

Original invoices, bank receipts, receiving records and reimbursement evidence arrive independently and out of order. The current confirmed-document upload and business-event model already stores immutable extractions, document links, voucher versions and audit rows, but event creation assumes all document roles are selected at once.

## Decision

Keep the existing business-event model as the long-lived case. Add a conservative, auditable path to start a pending event from one confirmed document and attach later confirmed documents, regardless of arrival order. Expense, purchase, asset-purchase and rental cases use the same intake and link lifecycle; their accounting requirements remain different. Read-only matching ranks candidates using **at least two** confirmed signals (amount, counterparty, date or contract number); its score is a heuristic ordering, **not** a calibrated probability. Every link requires human approval. Never mutate approved or exported voucher evidence when a late document arrives.

## Boundaries

- Reuse `DocumentAsset`, `DocumentExtraction`, `BusinessEvent`, `BusinessEventDocument`, voucher versioning and existing RBAC.
- Preserve invoice, bank receipt and goods receipt processing and existing reconciliation guards.
- Extend document types through a registry; recognizing a document is not the same as determining accounting treatment.
- No ERP write, no silent posting, no model-generated facts, and no new task broker in this increment.
- Recommended matches are limited to the latest 200 unlinked, confirmed documents in the same tenant/account. More extensive indexing, OCR normalization and asynchronous task queues are future increments, not claims of this change.
- A new piece of evidence cannot be attached directly if the case already has a non-rejected voucher. It remains in the document inbox pending explicit human resolution of the historic voucher.
