from __future__ import annotations

import json
from pathlib import Path

from boomai.core.models import ReviewComment, ReviewSummary, UsageStats, Severity
from boomai.review.estimation_history import EstimateFeatures
from boomai.review.estimator import ScanEstimate
from boomai.review.run_cost_report import write_run_cost_report


class DummyRuntimeModels:
    source = "live"
    fetched_at_utc = "2026-04-04T00:00:00Z"
    strong_mode = "auto"
    weak_mode = "auto"
    strong_model_id = "gemini-3.1-pro-preview"
    weak_model_id = "gemini-3.1-flash-lite-preview"
    strong_display_name = "Gemini 3.1 Pro Preview"
    weak_display_name = "Gemini 3.1 Flash-Lite Preview"


def test_cost_report_keeps_required_top_level_shape(tmp_path: Path) -> None:
    usage = UsageStats(prompt_tokens=10, completion_tokens=2, api_calls=1)
    usage.request_events.append(
        {
            "model": "gemini-3.1-pro-preview",
            "stage": "scan",
            "request_label": "[1/1]",
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "usage_metadata": {},
        }
    )
    review = ReviewSummary(
        summary="ok",
        findings=[ReviewComment(file="a.py", line=1, severity=Severity.MEDIUM, body="body")],
        usage=usage,
    )
    estimate = ScanEstimate(
        profile="default",
        model="gemini-3.1-pro-preview",
        model_label="Gemini 3.1 Pro Preview",
        patch_model="gemini-3.1-flash-lite-preview",
        patch_model_label="Gemini 3.1 Flash-Lite Preview",
        is_known_model=True,
        file_count=1,
        total_chars=10,
        chunk_count=1,
        total_api_calls_low=1,
        total_api_calls_high=2,
        input_tokens_low=10,
        input_tokens_high=20,
        output_tokens_low=3,
        output_tokens_high=5,
        cost_min=0.01,
        cost_max=0.02,
        time_min=1.0,
        time_max=2.0,
        features=EstimateFeatures(
            total_chars=10,
            file_count=1,
            chunk_count=1,
            api_calls_mid=1.0,
            input_tokens_mid=10.0,
            output_tokens_mid=3.0,
            base_cost_mid=0.015,
            base_time_mid=1.5,
            scan_model_flash=0,
            patch_model_flash=1,
            scan_profile_deep=0,
        ),
    )
    report_path = write_run_cost_report(
        repo_path=str(tmp_path),
        estimate=estimate,
        review=review,
        runtime_models=DummyRuntimeModels(),
        elapsed_seconds=1.0,
        applied_count=0,
        issue_seed_count=0,
        languages=["python"],
    )
    assert report_path is not None
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert "estimate" in payload
    assert "model_resolution" in payload
    assert "actual" in payload
    assert "pricing_notes" in payload
