"""Per-run cost attribution artifacts for BoomAI."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .estimator import ScanEstimate, compute_usage_cost_breakdown


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value).strip("-") or "run"


def _estimate_payload(estimate: ScanEstimate) -> dict[str, object]:
    return {
        "profile": estimate.profile,
        "model": estimate.model,
        "model_label": estimate.model_label,
        "patch_model": estimate.patch_model,
        "patch_model_label": estimate.patch_model_label,
        "file_count": estimate.file_count,
        "total_chars": estimate.total_chars,
        "chunk_count": estimate.chunk_count,
        "api_calls_low": estimate.total_api_calls_low,
        "api_calls_high": estimate.total_api_calls_high,
        "input_tokens_low": estimate.input_tokens_low,
        "input_tokens_high": estimate.input_tokens_high,
        "output_tokens_low": estimate.output_tokens_low,
        "output_tokens_high": estimate.output_tokens_high,
        "cost_min_usd": estimate.cost_min,
        "cost_max_usd": estimate.cost_max,
        "time_min_seconds": estimate.time_min,
        "time_max_seconds": estimate.time_max,
        "learned_samples": estimate.learned_samples,
        "features": asdict(estimate.features),
    }


def write_run_cost_report(
    *,
    repo_path: str,
    estimate: ScanEstimate,
    review,
    elapsed_seconds: float,
    applied_count: int,
    issue_seed_count: int,
    languages: list[str],
) -> Path | None:
    usage = getattr(review, "usage", None)
    if usage is None or usage.api_calls <= 0:
        return None

    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    report_dir = Path(repo_path).resolve() / ".boomai" / "runs"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{_safe_name(run_id)}-cost-report.json"

    cost_breakdown = compute_usage_cost_breakdown(usage)
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at_utc": _iso_now(),
        "repo_path": str(Path(repo_path).resolve()),
        "estimate": _estimate_payload(estimate),
        "actual": {
            "elapsed_seconds": elapsed_seconds,
            "findings_count": len(review.findings),
            "applied_count": applied_count,
            "issue_seed_count": issue_seed_count,
            "languages": languages,
            "summary": review.summary,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "api_calls": usage.api_calls,
                "usage_metadata_totals": usage.usage_metadata_totals,
                "per_model": usage.per_model,
                "per_stage": usage.per_stage,
                "per_stage_model": usage.per_stage_model,
                "request_events": usage.request_events,
            },
            "cost_breakdown": cost_breakdown,
        },
        "pricing_notes": {
            "display_cost_is_multiplied": True,
            "display_multiplier": cost_breakdown.get("display_multiplier", 1.0),
            "raw_cost_usd": cost_breakdown.get("raw_total_cost_usd", 0.0),
            "display_cost_usd": cost_breakdown.get("display_total_cost_usd", 0.0),
            "reference_urls": [
                "https://ai.google.dev/gemini-api/docs/pricing",
                "https://ai.google.dev/gemini-api/docs/models/gemini",
            ],
        },
    }

    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    latest_path = report_dir / "latest-cost-report.json"
    latest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return report_path
