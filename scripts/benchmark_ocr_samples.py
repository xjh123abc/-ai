"""Measure sequential OCR runtime and peak RSS for the local sample images.

Run from the repository root with:
    PYTHONPATH=services/api:packages services/api/.venv/bin/python scripts/benchmark_ocr_samples.py

The report contains timing and candidate counts only; it never writes OCR text or
recognized business fields unless a future change explicitly adds them.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import resource
import sys
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "services/api"))
sys.path.insert(0, str(REPOSITORY_ROOT / "packages"))

from app.ocr import engine, extract_image  # noqa: E402
from ingestion.candidates import field_candidates  # noqa: E402


def percentile(values: list[float], percent: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return round(ordered[rank - 1], 3)


def peak_rss_mib() -> float:
    # Linux reports KiB; macOS reports bytes.
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(value / (1024 if sys.platform.startswith("linux") else 1024 * 1024), 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--samples",
        type=Path,
        default=REPOSITORY_ROOT / "发票账单数据/zhangqing_raw_docs_20261025",
        help="Directory containing the 15 local JPG samples.",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON report path; defaults to stdout.")
    args = parser.parse_args()
    sample_root = args.samples.resolve()
    images = sorted(path for path in sample_root.rglob("*") if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"})
    if len(images) != 15:
        parser.error(f"Expected exactly 15 JPG samples under {sample_root}, found {len(images)}")

    model_started = time.perf_counter()
    engine()
    model_init_seconds = round(time.perf_counter() - model_started, 3)

    records = []
    durations = []
    provider = None
    model_version = None
    confidence_counts = {"below_0_5": 0, "0_5_to_0_8": 0, "at_least_0_8": 0, "missing": 0}
    total_started = time.perf_counter()
    for index, image_path in enumerate(images, start=1):
        started = time.perf_counter()
        extraction = extract_image(image_path)
        provider = extraction["provider"]
        model_version = extraction["model_version"]
        elapsed = time.perf_counter() - started
        durations.append(elapsed)
        fields = field_candidates(extraction["full_text"], extraction["lines"])
        for line in extraction["lines"]:
            confidence = line.get("confidence")
            if confidence is None:
                confidence_counts["missing"] += 1
            elif confidence < 0.5:
                confidence_counts["below_0_5"] += 1
            elif confidence < 0.8:
                confidence_counts["0_5_to_0_8"] += 1
            else:
                confidence_counts["at_least_0_8"] += 1
        records.append(
            {
                "sample_id": index,
                "group": image_path.parent.relative_to(sample_root).as_posix(),
                "elapsed_seconds": round(elapsed, 3),
                "line_count": len(extraction["lines"]),
                "candidate_field_count": sum(value.get("normalized_value") is not None for value in fields.values()),
            }
        )

    report = {
        "sample_count": len(records),
        "environment": {
            "platform": platform.platform(),
            "logical_cpu_count": os.cpu_count(),
        },
        "provider": provider,
        "model_version": model_version,
        "model_init_seconds": model_init_seconds,
        "sequential_wall_seconds": round(time.perf_counter() - total_started, 3),
        "per_image_seconds": records,
        "latency_seconds": {
            "mean": round(sum(durations) / len(durations), 3),
            "p50": percentile(durations, 50),
            "p95_nearest_rank": percentile(durations, 95),
        },
        "peak_process_rss_mib": peak_rss_mib(),
        "ocr_line_confidence_buckets": confidence_counts,
        "accuracy_note": "Candidate counts and OCR confidence are not accuracy metrics; gold labels are required.",
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
