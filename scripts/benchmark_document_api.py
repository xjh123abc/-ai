"""Measure sequential source-image uploads through the authenticated API.

Run from the repository root with:
    services/api/.venv/bin/python scripts/benchmark_document_api.py

The report contains latency, status, image size and coarse field-count metrics;
it never stores filenames, OCR text, candidate values, or image contents.
The benchmark uses an isolated SQLite database and excludes network/PostgreSQL
latency, so it is not a production-capacity estimate.
"""

from __future__ import annotations

import json
import math
import os
import platform
import resource
import shutil
import sys
import threading
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))
sys.path.insert(0, str(ROOT / "packages"))


def percentile(values: list[float], percent: int) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return round(ordered[rank - 1], 3)


def peak_rss_mib() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(value / (1024 if sys.platform.startswith("linux") else 1024 * 1024), 1)


def current_rss_mib() -> float | None:
    status = Path("/proc/self/status")
    if not status.exists():
        return None
    for line in status.read_text(encoding="ascii").splitlines():
        if line.startswith("VmRSS:"):
            return round(int(line.split()[1]) / 1024, 1)
    return None


def main() -> int:
    samples = ROOT / "发票账单数据/zhangqing_raw_docs_20261025"
    images = sorted(path for path in samples.rglob("*")
                    if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"})
    if len(images) != 15:
        raise SystemExit(f"Expected exactly 15 JPG samples under {samples}, found {len(images)}")

    runtime = Path(tempfile.mkdtemp(prefix="zhangqing-api-benchmark-"))
    os.environ["DATABASE_URL"] = f"sqlite:///{runtime / 'benchmark.sqlite3'}"
    os.environ["DEMO_MODE"] = "false"
    os.environ["STORAGE_DIR"] = str(runtime / "storage")
    rss_samples: list[float] = []
    stop_sampling = threading.Event()

    def sample_rss() -> None:
        while not stop_sampling.is_set():
            value = current_rss_mib()
            if value is not None:
                rss_samples.append(value)
            stop_sampling.wait(0.005)

    sampler = threading.Thread(target=sample_rss, name="rss-sampler", daemon=True)
    sampler.start()

    try:
        from fastapi.testclient import TestClient
        from sqlalchemy.orm import Session

        from app.db.base import Base
        from app.db.models import AccountSet, AppUser, Tenant, TenantMembership
        from app.db.session import engine
        from app.main import app
        from app.security import hash_password

        tenant_id = "benchmark-tenant"
        account_id = "benchmark-account-set"
        email = "benchmark@example.test"
        password = "API-benchmark-password-2026!"
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            tenant = Tenant(id=tenant_id, name="Isolated API benchmark", is_demo=False)
            account = AccountSet(id=account_id, tenant_id=tenant_id, name="Benchmark", currency="CNY")
            user = AppUser(email=email, display_name="API benchmark", password_hash=hash_password(password))
            db.add_all([tenant, account, user])
            db.flush()
            db.add(TenantMembership(user_id=user.id, tenant_id=tenant_id,
                                    account_set_id=account_id, role="ACCOUNTANT"))
            db.commit()

        client = TestClient(app)
        login = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        if login.status_code != 200:
            raise RuntimeError(f"Benchmark login failed with HTTP {login.status_code}")
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        measurements: list[dict] = []
        provider = None
        model_version = None
        for index, image_path in enumerate(images, start=1):
            started = time.perf_counter()
            with image_path.open("rb") as image:
                response = client.post("/api/v1/documents", headers=headers,
                    files={"file": (image_path.name, image, "image/jpeg")})
            elapsed = time.perf_counter() - started
            if response.status_code != 200:
                raise RuntimeError(f"Sample {index} upload failed with HTTP {response.status_code}")
            body = response.json()
            extraction = body.get("extraction") or {}
            provider = extraction.get("provider", provider)
            model_version = extraction.get("model_version", model_version)
            measurements.append({
                "sample_id": index,
                "group": image_path.parent.relative_to(samples).as_posix(),
                "byte_size": image_path.stat().st_size,
                "elapsed_seconds": round(elapsed, 3),
                "http_status": response.status_code,
                "processing_status": body.get("processing_status"),
                "process_rss_mib_after_request": current_rss_mib(),
                "candidate_field_count": sum(
                    field.get("normalized_value") is not None
                    for field in extraction.get("fields", {}).values()
                ),
                "duplicate": body.get("duplicate", False),
            })

        durations = [row["elapsed_seconds"] for row in measurements]
        warm_durations = durations[1:]
        report = {
            "sample_count": len(measurements),
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "database": "isolated SQLite",
                "transport": "FastAPI TestClient; no external network",
            },
            "provider": provider,
            "model_version": model_version,
            "measurements": measurements,
            "latency_seconds_including_upload_and_ocr": {
                "all_mean": round(sum(durations) / len(durations), 3),
                "all_p50": percentile(durations, 50),
                "all_p95_nearest_rank": percentile(durations, 95),
                "first_request_cold": durations[0],
                "warm_mean_excluding_first": round(sum(warm_durations) / len(warm_durations), 3),
                "warm_p50_excluding_first": percentile(warm_durations, 50),
                "warm_p95_nearest_rank_excluding_first": percentile(warm_durations, 95),
            },
            "peak_process_rss_mib": peak_rss_mib(),
            "peak_sampled_process_rss_mib": max(rss_samples, default=None),
            "rss_sampling_interval_ms": 5,
            "accuracy_note": "Candidate counts and response status are not accuracy metrics; gold labels are required.",
            "scope_note": "Includes multipart handling, local file write, OCR, candidate extraction, and SQLite persistence; excludes PostgreSQL and real network latency.",
        }
        output = ROOT / "tests/fixtures/reports/document-api-benchmark-2026-10-27.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {output.relative_to(ROOT)} ({len(measurements)} samples)")
        print(json.dumps(report["latency_seconds_including_upload_and_ocr"], indent=2))
        print(f"Peak RSS: {report['peak_process_rss_mib']} MiB")
        return 0
    finally:
        stop_sampling.set()
        sampler.join(timeout=1)
        try:
            from app.db.session import engine
            engine.dispose()
        except (ImportError, AttributeError):
            pass
        shutil.rmtree(runtime, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
