from pathlib import Path
from runpy import run_path


evaluate_sample = run_path(Path(__file__).resolve().parents[1] / "scripts/evaluate_ocr_gold.py")["evaluate_sample"]


def test_gold_evaluation_counts_exact_matches_and_keeps_unsupported_fields_out():
    score = evaluate_sample(
        {"fields": {"gross_amount": "45200.00", "purpose": "采购款"}},
        {"gross_amount": {"normalized_value": "45200.00"}},
    )

    assert score == {
        "correct": {"gross_amount": 1},
        "total": {"gross_amount": 1, "purpose": 1},
        "unsupported_fields": 0,
    }


def test_gold_evaluation_counts_missing_candidate_as_incorrect():
    score = evaluate_sample(
        {"fields": {"gross_amount": "45200.00"}},
        {"gross_amount": {"normalized_value": None}},
    )

    assert score["correct"] == {}
    assert score["total"] == {"gross_amount": 1}
