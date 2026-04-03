"""Per-run cost attribution artifacts for BoomAI."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..core.config import settings
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


def _scan_diagnostics_payload(request_events: list[dict[str, object]]) -> dict[str, object]:
    scan_events = [
        event for event in request_events
        if isinstance(event, dict) and str(event.get("stage", "")) == "scan"
    ]
    parse_status_counts: Counter[str] = Counter()
    finish_reason_counts: Counter[str] = Counter()
    suspicious_requests: list[dict[str, object]] = []

    for event in scan_events:
        extra = event.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        parse_status = str(extra.get("parse_status", "") or "")
        finish_reason = str(extra.get("candidate_finish_reason", "") or "")
        finish_message = str(extra.get("candidate_finish_message", "") or "")
        if parse_status:
            parse_status_counts[parse_status] += 1
        if finish_reason:
            finish_reason_counts[finish_reason] += 1

        suspicious = (
            parse_status in {"failed", "recovered"}
            or finish_reason not in {"", "STOP"}
        )
        if not suspicious:
            continue

        suspicious_requests.append(
            {
                "request_label": str(event.get("request_label", "") or ""),
                "model": str(event.get("model", "") or ""),
                "parse_status": parse_status or "unknown",
                "finish_reason": finish_reason or "",
                "finish_message": finish_message or "",
                "prompt_tokens": int(event.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(event.get("completion_tokens", 0) or 0),
                "chunk_chars": int(extra.get("chunk_chars", 0) or 0),
                "chunk_file_count": int(extra.get("chunk_file_count", 0) or 0),
                "user_message_chars": int(extra.get("user_message_chars", 0) or 0),
                "requested_output_tokens": int(extra.get("requested_output_tokens", 0) or 0),
                "system_prompt_chars": int(extra.get("system_prompt_chars", 0) or 0),
                "max_output_tokens": int(extra.get("max_output_tokens", 0) or 0),
                "thinking_config": extra.get("thinking_config"),
                "candidate_text_chars": int(extra.get("candidate_text_chars", 0) or 0),
                "recovered_findings_count": int(extra.get("recovered_findings_count", 0) or 0),
                "degraded_mode": bool(extra.get("degraded_mode", False)),
                "parse_retries_remaining": int(extra.get("parse_retries_remaining", 0) or 0),
            }
        )

    return {
        "scan_request_count": len(scan_events),
        "parse_status_counts": dict(parse_status_counts),
        "finish_reason_counts": dict(finish_reason_counts),
        "suspicious_scan_requests": suspicious_requests,
    }


def write_run_cost_report(
    *,
    repo_path: str,
    estimate: ScanEstimate,
    review,
    runtime_models=None,
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
        "schema_version": 2,
        "run_id": run_id,
        "created_at_utc": _iso_now(),
        "repo_path": str(Path(repo_path).resolve()),
        "estimate": _estimate_payload(estimate),
        "model_resolution": (
            {
                "source": getattr(runtime_models, "source", ""),
                "fetched_at_utc": getattr(runtime_models, "fetched_at_utc", None),
                "strong_mode": getattr(runtime_models, "strong_mode", "auto"),
                "weak_mode": getattr(runtime_models, "weak_mode", "auto"),
                "strong_model_id": getattr(runtime_models, "strong_model_id", estimate.model),
                "weak_model_id": getattr(runtime_models, "weak_model_id", estimate.patch_model),
                "strong_display_name": getattr(runtime_models, "strong_display_name", estimate.model_label),
                "weak_display_name": getattr(runtime_models, "weak_display_name", estimate.patch_model_label),
            }
            if runtime_models is not None else None
        ),
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
            "diagnostics": _scan_diagnostics_payload(usage.request_events),
        },
        "pricing_notes": {
            "display_cost_is_multiplied": cost_breakdown.get("display_multiplier", 1.0) != 1.0,
            "display_multiplier": cost_breakdown.get("display_multiplier", 1.0),
            "raw_cost_usd": cost_breakdown.get("raw_total_cost_usd", 0.0),
            "display_cost_usd": cost_breakdown.get("display_total_cost_usd", 0.0),
            "billing_currency": str(settings.billing_currency).upper(),
            "usd_to_eur_rate": float(settings.usd_to_eur_rate),
            "raw_cost_eur_approx": cost_breakdown.get("raw_total_cost_usd", 0.0) * float(settings.usd_to_eur_rate),
            "display_cost_eur_approx": cost_breakdown.get("display_total_cost_usd", 0.0) * float(settings.usd_to_eur_rate),
            "reference_urls": [
                "https://ai.google.dev/gemini-api/docs/pricing",
                "https://data.ecb.europa.eu/currency-converter",
                "https://ai.google.dev/gemini-api/docs/models/gemini",
            ],
        },
    }

    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    latest_path = report_dir / "latest-cost-report.json"
    latest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return report_path
