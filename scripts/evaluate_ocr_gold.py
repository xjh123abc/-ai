"""Evaluate current OCR field candidates against the reviewed 15-image gold set.

Run from the repository root:
    PYTHONPATH=services/api:packages services/api/.venv/bin/python scripts/evaluate_ocr_gold.py

The report intentionally contains aggregate counts only, not recognized ticket data.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))
sys.path.insert(0, str(ROOT / "packages"))

from app.ocr import extract_image  # noqa: E402
from ingestion.candidates import field_candidates  # noqa: E402

GOLD_PATH = ROOT / "tests/fixtures/expected/all_15_samples.json"
# Canonical gold field -> current candidate field. Unsupported gold fields are
# reported separately and never counted as correct.
FIELD_MAP = {
    "invoice_number": "invoice_number", "issue_date": "issue_date",
    "buyer": "buyer", "seller": "seller", "purpose": "purpose", "gross_amount": "gross_amount",
    "payer": "payer", "payee": "payee",
    "payment_date": "payment_date",
    "settlement_date": "settlement_date",
    "receipt_number": "receipt_number", "receipt_date": "receipt_date", "item": "item",
    "acceptance_date": "acceptance_date", "acceptance_result": "acceptance_result",
    "contract_number": "contract_number",
    "settled": "settled",
    "net_amount": "net_amount", "tax_amount": "tax_amount", "tax_rate": "tax_rate",
    "quantity": "quantity",
}


def evaluate_sample(expected: dict, candidates: dict) -> dict:
    correct = defaultdict(int)
    total = defaultdict(int)
    unsupported = 0
    for field, expected_value in expected["fields"].items():
        candidate_key = FIELD_MAP.get(field)
        if candidate_key is None:
            unsupported += 1
            continue
        total[field] += 1
        actual = candidates.get(candidate_key, {}).get("normalized_value")
        if actual is not None and str(actual).strip() == str(expected_value).strip():
            correct[field] += 1
    return {"correct": dict(correct), "total": dict(total), "unsupported_fields": unsupported}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=ROOT / "发票账单数据/zhangqing_raw_docs_20261025")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sample_root = args.samples.resolve()
    gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    results = defaultdict(lambda: {"correct": 0, "total": 0})
    unsupported = defaultdict(int)
    classified_correct = 0
    for sample in gold["samples"]:
        image = sample_root / sample["file"]
        if not image.is_file():
            parser.error(f"Gold sample is missing: {image}")
        extraction = extract_image(image)
        candidates = field_candidates(extraction["full_text"], extraction["lines"])
        scored = evaluate_sample(sample, candidates)
        predicted_type = candidates.get("document_type_candidate", {}).get("normalized_value")
        if predicted_type == sample["document_type"]:
            classified_correct += 1
        unsupported[sample["document_type"]] += scored["unsupported_fields"]
        for field, count in scored["total"].items():
            results[field]["correct"] += scored["correct"].get(field, 0)
            results[field]["total"] += count
    fields = {
        name: {**counts, "accuracy": round(counts["correct"] / counts["total"], 4) if counts["total"] else None}
        for name, counts in sorted(results.items())
    }
    report = {
        "gold_sample_count": len(gold["samples"]),
        "field_accuracy_exact_match": fields,
        "supported_field_count": sum(item["total"] for item in fields.values()),
        "unsupported_gold_field_count": sum(unsupported.values()),
        "document_type_classification": {"implemented": True, "correct": classified_correct,
            "total": len(gold["samples"]), "accuracy": round(classified_correct / len(gold["samples"]), 4)},
        "limitations": "Type classification is a rule-based candidate and remains NEEDS_REVIEW; exact match is measured on this 15-image set only. Unsupported gold fields are reported as gaps, not successes. No source text or field values are included.",
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
