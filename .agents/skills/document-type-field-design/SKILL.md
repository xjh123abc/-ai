---
name: document-type-field-design
description: Design and implement a new OCR document type, its business-critical fields, and evidence-based field matching in this project.
---

# Add an OCR document type

Use this skill when the user provides a new kind of business document and asks to make the system recognize it. Complete the implementation and validate it against the supplied document; a proposal or a new dropdown option alone is not completion.

## Workflow

1. **Understand the document's job.** Read the user's requirement and inspect the supplied original and OCR output. Determine what business decision, reconciliation, or accounting work this document supports. Distinguish its document type from the business event type and document role. Search for an existing type with the same meaning before adding a new one. Ask only for information that cannot be inferred and would change the field design or workflow.

2. **Trace the current pipeline.** Follow type classification, OCR candidate extraction, backend field validation and summaries, frontend type selection and field rendering, and any role or downstream reconciliation rules affected by the new type. In this repository, begin with `packages/ingestion/candidates.py`, `services/api/app/routers/workflow.py`, and `apps/web/src/App.vue`; follow imports and callers to find all required integration points.

3. **Design the core fields from business use.** Choose every field needed to identify, reconcile, or account for this document type. The field count is determined by the business workflow, not by a preference for fewer fields. For each field, establish its meaning, canonical key (reuse an existing key when its meaning matches), and the business use that requires it. Leave descriptive details, signatures, and other source content in the OCR text unless a workflow actually depends on them. Do not create a structured field merely because a label appears in the document.

4. **Map OCR evidence to those fields.** Inspect the supplied OCR text and positioned lines against the original. Identify the value cues for each core field, including legitimate label variants and layout patterns. Handle OCR-split labels, adjacent values, and normalization only where supported by evidence. Preserve evidence text, page, and coordinates on candidates. Keep absent or ambiguous values unresolved for human review; never fill them from guesses. If OCR text contains a core value but the candidate is empty, improve the mapping before calling the sample complete.

5. **Implement the type end to end.** Add the document type to classification and display names/groups. Add its core field keys and labels to the frontend. Add the backend type allowlist and appropriate required fields, plus candidate extraction and normalization. Update document-role compatibility, business reconciliation, or other downstream rules when the new type participates in them. Keep candidate values unconfirmed until a user reviews and saves them. Keep unrelated files and workflows untouched.

6. **Validate recognition and field population together.** Use the supplied document as the acceptance example. Re-run or recompute candidates using the new type and check both that the document type is recognized and that every core field supported by the original has a value with matching evidence. If any supported core value is missing, diagnose and fix the responsible stage, then repeat the check. For a value absent or unreadable in the source, report that limitation instead of counting it as a successful extraction. Check that the frontend shows the designed core fields and that the API accepts the new type and fields. Confirm the running API has loaded the modified code before judging endpoint results. Run focused existing tests or add/update a focused regression test when needed to cover a discovered mapping rule.

7. **Report what was verified.** Summarize the new type, its core field keys and business purpose, the sample values actually recognized, unresolved source values, changed files, and focused checks run. Do not claim completion based only on static configuration or an available type selector.

## Completion criteria

The task is complete only when the new type can be selected or suggested, the backend accepts it, its core fields appear in the review UI, and the supplied sample demonstrates type recognition plus evidence-backed values for every readable core field. Human confirmation remains a separate step.
