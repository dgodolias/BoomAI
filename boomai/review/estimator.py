"""Local cost and time estimation for BoomAI scans.

Estimates the two-stage BoomAI pipeline:
1. findings-only scan
2. targeted patch generation for a subset of findings
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..core.policies import build_estimate_policy
from ..core.google_pricing import ModelPricing, get_pricing
from .estimation_history import EstimateFeatures, get_record_count, learn_adjustment
from ..presentation.estimate_output import format_estimate as present_estimate
from .prompts import build_fix_system_prompt, build_plan_prompt, build_scan_system_prompt
from .runtime_policy import (
    compute_patch_concurrency,
    compute_scan_concurrency,
    compute_scan_output_tokens,
    normalize_scan_output_tokens,
)
from .services.chunk_planner import ChunkPlanner
from .services.cost_attribution import (
    compute_usage_cost_breakdown as service_compute_usage_cost_breakdown,
    format_actual_cost as service_format_actual_cost,
)


def _estimate_plan_billed_output_tokens(plan_output_tokens: int, chunk_count: int) -> tuple[int, int]:
    low = min(plan_output_tokens, max(256, 220 + chunk_count * 60))
    high = min(plan_output_tokens, max(low, 420 + chunk_count * 120))
    return low, high


def _estimate_scan_billed_output_tokens(
    model_name: str,
    output_cap: int,
    *,
    chunk_chars: int,
    file_count: int,
    issue_seed_count: int,
) -> tuple[int, int]:
    lowered = model_name.lower()
    is_gemini3 = "gemini-3" in lowered or "gemini-3.1" in lowered
    is_flash = "flash" in lowered
    is_pro = "pro" in lowered and not is_flash

    if is_gemini3 and is_pro:
        if output_cap <= 8192 or chunk_chars <= 10_000 or file_count <= 5:
            low_ratio, high_ratio = 0.55, 0.98
        elif chunk_chars <= 40_000 or file_count <= 10:
            low_ratio, high_ratio = 0.40, 0.85
        else:
            low_ratio, high_ratio = 0.30, 0.75
    elif is_gemini3 and is_flash:
        low_ratio, high_ratio = 0.08, 0.28
    elif is_flash:
        low_ratio, high_ratio = 0.06, 0.22
    else:
        low_ratio, high_ratio = 0.12, 0.40

    if issue_seed_count >= 4:
        low_ratio = min(0.98, low_ratio + 0.05)
        high_ratio = min(0.99, high_ratio + 0.05)

    low = int(output_cap * low_ratio)
    high = int(output_cap * high_ratio)
    return max(1, low), max(max(1, low), high)


def _estimate_patch_output_tokens(model_name: str, output_cap: int, patch_calls: int, ratio: float) -> int:
    lowered = model_name.lower()
    if "gemini-3" in lowered or "gemini-3.1" in lowered:
        if "flash" in lowered:
            effective_ratio = ratio
        else:
            effective_ratio = max(ratio, 0.10)
    elif "flash" in lowered:
        effective_ratio = max(ratio, 0.06)
    else:
        effective_ratio = max(ratio, 0.10)
    return int(patch_calls * output_cap * effective_ratio)


@dataclass
class ScanEstimate:
    profile: str
    model: str
    model_label: str
    patch_model: str
    patch_model_label: str
    is_known_model: bool
    file_count: int
    total_chars: int
    chunk_count: int
    total_api_calls_low: int
    total_api_calls_high: int
    input_tokens_low: int
    input_tokens_high: int
    output_tokens_low: int
    output_tokens_high: int
    cost_min: float
    cost_max: float
    time_min: float
    time_max: float
    features: EstimateFeatures
    learned_samples: int = 0
    learned_blended: bool = False
    recorded_samples: int = 0


def estimate_scan(
    file_contents: list[tuple[str, str]],
    model: str,
    patch_model: str,
    max_scan_chars: int,
    scan_output_tokens: int,
    plan_output_tokens: int,
    profile: str = "default",
    patch_max_findings_per_chunk: int = 5,
    languages: list[str] | None = None,
    model_label: str | None = None,
    patch_model_label: str | None = None,
) -> ScanEstimate:
    """Estimate cost and time for the full two-stage scan."""
    estimate_policy = build_estimate_policy()
    chunk_planner = ChunkPlanner()
    total_chars = sum(len(c) for _, c in file_contents)
    chunks = chunk_planner.chunk_files(file_contents, max_scan_chars)
    chunk_count = len(chunks)

    langs = languages or []
    scan_system_chars = len(build_scan_system_prompt(langs))
    plan_system_chars = len(build_plan_prompt(max_scan_chars))
    fix_system_chars = len(build_fix_system_prompt())

    plan_input = int(plan_system_chars / estimate_policy.chars_per_token)
    scan_input = 0
    scan_output_low, scan_output_high = _estimate_plan_billed_output_tokens(plan_output_tokens, chunk_count)
    for chunk in chunks:
        chunk_chars = sum(len(c) for _, c in chunk)
        scan_input += int((scan_system_chars + chunk_chars + 300) / estimate_policy.chars_per_token)
        requested_chunk_cap = compute_scan_output_tokens(chunk_chars, len(chunk))
        chunk_cap = normalize_scan_output_tokens(model, requested_chunk_cap)
        billed_low, billed_high = _estimate_scan_billed_output_tokens(
            model,
            chunk_cap,
            chunk_chars=chunk_chars,
            file_count=len(chunk),
            issue_seed_count=0,
        )
        scan_output_low += billed_low
        scan_output_high += billed_high

    base_patch_calls_low = max(chunk_count, math.ceil(total_chars / 75_000))
    base_patch_calls_high = max(base_patch_calls_low, math.ceil(total_chars / 25_000))
    coverage_multiplier = 1.0
    if profile == "deep":
        coverage_multiplier = 1.25
    patch_budget_multiplier = max(1.0, patch_max_findings_per_chunk / 5.0)
    patch_multiplier = coverage_multiplier * (1.0 + ((patch_budget_multiplier - 1.0) * 0.35))
    patch_calls_low = max(chunk_count, math.ceil(base_patch_calls_low * patch_multiplier))
    patch_calls_high = max(patch_calls_low, math.ceil(base_patch_calls_high * patch_multiplier))
    patch_prompt_chars_low = fix_system_chars + 4_500
    patch_prompt_chars_high = fix_system_chars + 10_500
    patch_input_low = int((patch_calls_low * patch_prompt_chars_low) / estimate_policy.chars_per_token)
    patch_input_high = int((patch_calls_high * patch_prompt_chars_high) / estimate_policy.chars_per_token)
    patch_output_cap = min(scan_output_tokens, 4096)
    patch_output_low = _estimate_patch_output_tokens(
        patch_model,
        patch_output_cap,
        patch_calls_low,
        estimate_policy.patch_output_ratio_low,
    )
    patch_output_high = _estimate_patch_output_tokens(
        patch_model,
        patch_output_cap,
        patch_calls_high,
        estimate_policy.patch_output_ratio_high,
    )

    total_input_low = plan_input + scan_input + patch_input_low
    total_input_high = plan_input + scan_input + patch_input_high
    total_output_low = scan_output_low + patch_output_low
    total_output_high = scan_output_high + patch_output_high

    pricing, is_known = get_pricing(model)
    patch_pricing, _ = get_pricing(patch_model)
    cost_min = (
        (plan_input + scan_input) / 1_000_000 * pricing.input_per_m
        + scan_output_low / 1_000_000 * pricing.output_per_m
        + patch_input_low / 1_000_000 * patch_pricing.input_per_m
        + patch_output_low / 1_000_000 * patch_pricing.output_per_m
    )
    cost_max = (
        (plan_input + scan_input) / 1_000_000 * pricing.input_per_m
        + scan_output_high / 1_000_000 * pricing.output_per_m
        + patch_input_high / 1_000_000 * patch_pricing.input_per_m
        + patch_output_high / 1_000_000 * patch_pricing.output_per_m
    )

    scan_per_call = (
        estimate_policy.flash_time_per_call_seconds
        if "flash" in model.lower()
        else estimate_policy.pro_time_per_call_seconds
    )
    patch_per_call = (
        estimate_policy.flash_patch_time_per_call_seconds
        if "flash" in patch_model.lower()
        else estimate_policy.pro_patch_time_per_call_seconds
    )
    scan_concurrency = compute_scan_concurrency(model, chunk_count)
    patch_concurrency = compute_patch_concurrency(patch_model, patch_calls_high)
    scan_batches = math.ceil(chunk_count / scan_concurrency)
    patch_batches_low = math.ceil(patch_calls_low / patch_concurrency)
    patch_batches_high = math.ceil(patch_calls_high / patch_concurrency)
    time_min = estimate_policy.plan_time_seconds + scan_batches * scan_per_call * 0.35 + patch_batches_low * patch_per_call
    time_max = estimate_policy.plan_time_seconds + scan_batches * scan_per_call + patch_batches_high * patch_per_call
    base_cost_mid = (cost_min + cost_max) / 2.0
    base_time_mid = (time_min + time_max) / 2.0
    features = EstimateFeatures(
        total_chars=total_chars,
        file_count=len(file_contents),
        chunk_count=chunk_count,
        api_calls_mid=(1 + chunk_count + ((patch_calls_low + patch_calls_high) / 2.0)),
        input_tokens_mid=(total_input_low + total_input_high) / 2.0,
        output_tokens_mid=(total_output_low + total_output_high) / 2.0,
        base_cost_mid=base_cost_mid,
        base_time_mid=base_time_mid,
        scan_model_flash=int("flash" in model.lower()),
        patch_model_flash=int("flash" in patch_model.lower()),
        scan_profile_deep=int(profile == "deep"),
    )
    learned = learn_adjustment(features)
    learned_samples = 0
    learned_blended = False
    recorded_samples = 0
    if learned is not None:
        cost_min = learned.cost_min
        cost_max = learned.cost_max
        time_min = learned.time_min
        time_max = learned.time_max
        learned_samples = learned.samples
        learned_blended = learned.blended
        recorded_samples = learned.samples
    else:
        recorded_samples = get_record_count()

    return ScanEstimate(
        profile=profile,
        model=model,
        model_label=model_label or pricing.label,
        patch_model=patch_model,
        patch_model_label=patch_model_label or patch_pricing.label,
        is_known_model=is_known,
        file_count=len(file_contents),
        total_chars=total_chars,
        chunk_count=chunk_count,
        total_api_calls_low=1 + chunk_count + patch_calls_low,
        total_api_calls_high=1 + chunk_count + patch_calls_high,
        input_tokens_low=total_input_low,
        input_tokens_high=total_input_high,
        output_tokens_low=total_output_low,
        output_tokens_high=total_output_high,
        cost_min=cost_min,
        cost_max=cost_max,
        time_min=time_min,
        time_max=time_max,
        features=features,
        learned_samples=learned_samples,
        learned_blended=learned_blended,
        recorded_samples=recorded_samples,
    )


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"


def _fmt_cost(v: float) -> str:
    if v < 0.01:
        return "< $0.01"
    return f"${v:.2f}"


def _fmt_eur(v_usd: float) -> str:
    eur = v_usd * float(settings.usd_to_eur_rate)
    if eur < 0.01:
        return "< €0.01"
    return f"€{eur:.2f}"


def _event_cost(event: dict[str, object]) -> dict[str, float]:
    model_name = str(event.get("model", "") or "")
    pricing, _ = get_pricing(model_name)
    usage_meta = event.get("usage_metadata", {}) or {}
    if not isinstance(usage_meta, dict):
        usage_meta = {}

    prompt_tokens = int(event.get("prompt_tokens", 0) or 0)
    completion_tokens = int(event.get("completion_tokens", 0) or 0)
    cached_tokens = int(usage_meta.get("cachedContentTokenCount", 0) or 0)
    thinking_tokens = int(usage_meta.get("thoughtsTokenCount", 0) or 0)
    billed_output_tokens = completion_tokens + thinking_tokens
    noncached_prompt_tokens = max(0, prompt_tokens - cached_tokens)
    input_rate, output_rate, cached_rate = effective_rates(pricing, prompt_tokens)

    input_cost = noncached_prompt_tokens / 1_000_000 * input_rate
    cached_input_cost = cached_tokens / 1_000_000 * cached_rate
    output_cost = billed_output_tokens / 1_000_000 * output_rate

    return {
        "prompt_tokens": prompt_tokens,
        "noncached_prompt_tokens": noncached_prompt_tokens,
        "cached_prompt_tokens": cached_tokens,
        "completion_tokens": completion_tokens,
        "thinking_tokens": thinking_tokens,
        "billed_output_tokens": billed_output_tokens,
        "input_cost_usd": input_cost,
        "cached_input_cost_usd": cached_input_cost,
        "output_cost_usd": output_cost,
        "raw_cost_usd": input_cost + cached_input_cost + output_cost,
    }


def compute_usage_cost_breakdown(usage) -> dict[str, object]:
    """Return raw and display cost attribution for a UsageStats object."""
    return service_compute_usage_cost_breakdown(usage)


def format_estimate(est: ScanEstimate) -> None:
    present_estimate(est)


def format_actual_cost(usage) -> str:
    """Format actual cost line using per-model token accounting."""
    return service_format_actual_cost(usage)
