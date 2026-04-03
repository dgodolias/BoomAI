"""Local cost and time estimation for BoomAI scans.

Estimates the two-stage BoomAI pipeline:
1. findings-only scan
2. targeted patch generation for a subset of findings
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..core.config import settings
from .estimation_history import EstimateFeatures, learn_adjustment

CHARS_PER_TOKEN = 3.7
PATCH_OUTPUT_RATIO_LOW = 0.05
PATCH_OUTPUT_RATIO_HIGH = 0.18

_PLAN_TIME_S = 15.0
_PRO_TIME_PER_CALL_S = 120.0
_FLASH_TIME_PER_CALL_S = 30.0
_PRO_PATCH_TIME_PER_CALL_S = 35.0
_FLASH_PATCH_TIME_PER_CALL_S = 12.0
_DISPLAY_COST_MULTIPLIER = 1.0
_HIGH_CONTEXT_THRESHOLD = 200_000


@dataclass(frozen=True)
class ModelPricing:
    input_per_m: float
    output_per_m: float
    label: str
    cached_input_per_m: float | None = None
    input_per_m_high: float | None = None
    output_per_m_high: float | None = None
    cached_input_per_m_high: float | None = None


PRICING: list[tuple[str, ModelPricing]] = [
    (
        "gemini-3.1-pro-preview",
        ModelPricing(
            2.00,
            12.00,
            "Gemini 3.1 Pro Preview",
            cached_input_per_m=0.20,
            input_per_m_high=4.00,
            output_per_m_high=18.00,
            cached_input_per_m_high=0.40,
        ),
    ),
    (
        "gemini-3.1-flash-lite-preview",
        ModelPricing(
            0.25,
            1.50,
            "Gemini 3.1 Flash-Lite Preview",
            cached_input_per_m=0.025,
            input_per_m_high=0.25,
            output_per_m_high=1.50,
            cached_input_per_m_high=0.025,
        ),
    ),
    ("gemini-3-pro-preview", ModelPricing(1.25, 10.00, "Gemini 3 Pro Preview")),
    ("gemini-3-flash-preview", ModelPricing(0.50, 3.00, "Gemini 3 Flash Preview")),
    ("gemini-2.5-pro", ModelPricing(1.25, 10.00, "Gemini 2.5 Pro")),
    ("gemini-2.5-flash", ModelPricing(0.30, 2.50, "Gemini 2.5 Flash")),
    ("gemini-2.5-flash-lite-preview-09-2025", ModelPricing(0.10, 0.40, "Gemini 2.5 Flash-Lite Preview")),
]

_UNKNOWN = ModelPricing(1.25, 10.00, "Unknown model")


def get_pricing(model_id: str) -> tuple[ModelPricing, bool]:
    for prefix, pricing in PRICING:
        if model_id.startswith(prefix):
            return pricing, True
    return _UNKNOWN, False


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
) -> ScanEstimate:
    """Estimate cost and time for the full two-stage scan."""
    from boomai.review.gemini_review import (
        _chunk_files,
        _compute_patch_concurrency,
        _compute_scan_concurrency,
        _compute_scan_output_tokens,
        _normalize_scan_output_tokens,
    )
    from boomai.review.prompts import (
        build_fix_system_prompt,
        build_plan_prompt,
        build_scan_system_prompt,
    )

    total_chars = sum(len(c) for _, c in file_contents)
    chunks = _chunk_files(file_contents, max_scan_chars)
    chunk_count = len(chunks)

    langs = languages or []
    scan_system_chars = len(build_scan_system_prompt(langs))
    plan_system_chars = len(build_plan_prompt(max_scan_chars))
    fix_system_chars = len(build_fix_system_prompt())

    plan_input = int(plan_system_chars / CHARS_PER_TOKEN)
    scan_input = 0
    scan_output_low, scan_output_high = _estimate_plan_billed_output_tokens(plan_output_tokens, chunk_count)
    for chunk in chunks:
        chunk_chars = sum(len(c) for _, c in chunk)
        scan_input += int((scan_system_chars + chunk_chars + 300) / CHARS_PER_TOKEN)
        requested_chunk_cap = _compute_scan_output_tokens(chunk_chars, len(chunk))
        chunk_cap = _normalize_scan_output_tokens(model, requested_chunk_cap)
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
    patch_input_low = int((patch_calls_low * patch_prompt_chars_low) / CHARS_PER_TOKEN)
    patch_input_high = int((patch_calls_high * patch_prompt_chars_high) / CHARS_PER_TOKEN)
    patch_output_cap = min(scan_output_tokens, 4096)
    patch_output_low = _estimate_patch_output_tokens(
        patch_model,
        patch_output_cap,
        patch_calls_low,
        PATCH_OUTPUT_RATIO_LOW,
    )
    patch_output_high = _estimate_patch_output_tokens(
        patch_model,
        patch_output_cap,
        patch_calls_high,
        PATCH_OUTPUT_RATIO_HIGH,
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

    scan_per_call = _FLASH_TIME_PER_CALL_S if "flash" in model.lower() else _PRO_TIME_PER_CALL_S
    patch_per_call = _FLASH_PATCH_TIME_PER_CALL_S if "flash" in patch_model.lower() else _PRO_PATCH_TIME_PER_CALL_S
    scan_concurrency = _compute_scan_concurrency(model, chunk_count)
    patch_concurrency = _compute_patch_concurrency(patch_model, patch_calls_high)
    scan_batches = math.ceil(chunk_count / scan_concurrency)
    patch_batches_low = math.ceil(patch_calls_low / patch_concurrency)
    patch_batches_high = math.ceil(patch_calls_high / patch_concurrency)
    time_min = _PLAN_TIME_S + scan_batches * scan_per_call * 0.35 + patch_batches_low * patch_per_call
    time_max = _PLAN_TIME_S + scan_batches * scan_per_call + patch_batches_high * patch_per_call
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
    if learned is not None:
        cost_min = learned.cost_min
        cost_max = learned.cost_max
        time_min = learned.time_min
        time_max = learned.time_max
        learned_samples = learned.samples

    return ScanEstimate(
        profile=profile,
        model=model,
        model_label=pricing.label,
        patch_model=patch_model,
        patch_model_label=patch_pricing.label,
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


def _effective_rates(pricing: ModelPricing, prompt_tokens: int) -> tuple[float, float, float]:
    use_high = prompt_tokens > _HIGH_CONTEXT_THRESHOLD
    input_rate = pricing.input_per_m_high if use_high and pricing.input_per_m_high is not None else pricing.input_per_m
    output_rate = pricing.output_per_m_high if use_high and pricing.output_per_m_high is not None else pricing.output_per_m
    cached_rate = (
        pricing.cached_input_per_m_high
        if use_high and pricing.cached_input_per_m_high is not None
        else pricing.cached_input_per_m
    )
    if cached_rate is None:
        cached_rate = input_rate
    return input_rate, output_rate, cached_rate


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
    input_rate, output_rate, cached_rate = _effective_rates(pricing, prompt_tokens)

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
    request_events = list(getattr(usage, "request_events", []))
    per_model: dict[str, dict[str, object]] = {}
    total_input_cost = 0.0
    total_cached_input_cost = 0.0
    total_output_cost = 0.0
    per_request: list[dict[str, object]] = []

    for event in request_events:
        if not isinstance(event, dict):
            continue
        model_name = str(event.get("model", "") or "")
        stage_name = str(event.get("stage", "unknown") or "unknown")
        pricing, known = get_pricing(model_name)
        cost = _event_cost(event)
        total_input_cost += cost["input_cost_usd"]
        total_cached_input_cost += cost["cached_input_cost_usd"]
        total_output_cost += cost["output_cost_usd"]

        model_bucket = per_model.setdefault(
            model_name,
            {
                "label": pricing.label,
                "known_pricing": known,
                "prompt_tokens": 0,
                "noncached_prompt_tokens": 0,
                "cached_prompt_tokens": 0,
                "completion_tokens": 0,
                "thinking_tokens": 0,
                "billed_output_tokens": 0,
                "api_calls": 0,
                "input_cost_usd": 0.0,
                "cached_input_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "raw_cost_usd": 0.0,
            },
        )
        for key in (
            "prompt_tokens",
            "noncached_prompt_tokens",
            "cached_prompt_tokens",
            "completion_tokens",
            "thinking_tokens",
            "billed_output_tokens",
        ):
            model_bucket[key] += int(cost[key])
        model_bucket["api_calls"] += 1
        model_bucket["input_cost_usd"] += cost["input_cost_usd"]
        model_bucket["cached_input_cost_usd"] += cost["cached_input_cost_usd"]
        model_bucket["output_cost_usd"] += cost["output_cost_usd"]
        model_bucket["raw_cost_usd"] += cost["raw_cost_usd"]

        per_request.append(
            {
                "stage": stage_name,
                "model": model_name,
                "request_label": str(event.get("request_label", "") or ""),
                "prompt_tokens": int(cost["prompt_tokens"]),
                "noncached_prompt_tokens": int(cost["noncached_prompt_tokens"]),
                "cached_prompt_tokens": int(cost["cached_prompt_tokens"]),
                "completion_tokens": int(cost["completion_tokens"]),
                "thinking_tokens": int(cost["thinking_tokens"]),
                "billed_output_tokens": int(cost["billed_output_tokens"]),
                "input_cost_usd": cost["input_cost_usd"],
                "cached_input_cost_usd": cost["cached_input_cost_usd"],
                "output_cost_usd": cost["output_cost_usd"],
                "raw_cost_usd": cost["raw_cost_usd"],
            }
        )

    per_stage: dict[str, dict[str, object]] = {}
    for event in request_events:
        if not isinstance(event, dict):
            continue
        stage_name = str(event.get("stage", "unknown") or "unknown")
        model_name = str(event.get("model", "") or "")
        pricing, known = get_pricing(model_name)
        cost = _event_cost(event)
        stage_bucket = per_stage.setdefault(
            stage_name,
            {
                "prompt_tokens": 0,
                "noncached_prompt_tokens": 0,
                "cached_prompt_tokens": 0,
                "completion_tokens": 0,
                "thinking_tokens": 0,
                "billed_output_tokens": 0,
                "api_calls": 0,
                "input_cost_usd": 0.0,
                "cached_input_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "raw_cost_usd": 0.0,
                "models": {},
            },
        )
        for key in (
            "prompt_tokens",
            "noncached_prompt_tokens",
            "cached_prompt_tokens",
            "completion_tokens",
            "thinking_tokens",
            "billed_output_tokens",
        ):
            stage_bucket[key] += int(cost[key])
        stage_bucket["api_calls"] += 1
        stage_bucket["input_cost_usd"] += cost["input_cost_usd"]
        stage_bucket["cached_input_cost_usd"] += cost["cached_input_cost_usd"]
        stage_bucket["output_cost_usd"] += cost["output_cost_usd"]
        stage_bucket["raw_cost_usd"] += cost["raw_cost_usd"]

        stage_model_bucket = stage_bucket["models"].setdefault(
            model_name,
            {
                "label": pricing.label,
                "known_pricing": known,
                "prompt_tokens": 0,
                "noncached_prompt_tokens": 0,
                "cached_prompt_tokens": 0,
                "completion_tokens": 0,
                "thinking_tokens": 0,
                "billed_output_tokens": 0,
                "api_calls": 0,
                "input_cost_usd": 0.0,
                "cached_input_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "raw_cost_usd": 0.0,
            },
        )
        for key in (
            "prompt_tokens",
            "noncached_prompt_tokens",
            "cached_prompt_tokens",
            "completion_tokens",
            "thinking_tokens",
            "billed_output_tokens",
        ):
            stage_model_bucket[key] += int(cost[key])
        stage_model_bucket["api_calls"] += 1
        stage_model_bucket["input_cost_usd"] += cost["input_cost_usd"]
        stage_model_bucket["cached_input_cost_usd"] += cost["cached_input_cost_usd"]
        stage_model_bucket["output_cost_usd"] += cost["output_cost_usd"]
        stage_model_bucket["raw_cost_usd"] += cost["raw_cost_usd"]

    raw_cost = total_input_cost + total_cached_input_cost + total_output_cost
    displayed_cost = raw_cost * _DISPLAY_COST_MULTIPLIER
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0)),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0)),
        "api_calls": int(getattr(usage, "api_calls", 0)),
        "usage_metadata_totals": dict(getattr(usage, "usage_metadata_totals", {})),
        "raw_input_cost_usd": total_input_cost,
        "raw_cached_input_cost_usd": total_cached_input_cost,
        "raw_output_cost_usd": total_output_cost,
        "raw_total_cost_usd": raw_cost,
        "display_multiplier": _DISPLAY_COST_MULTIPLIER,
        "display_total_cost_usd": displayed_cost,
        "per_model": per_model,
        "per_stage": per_stage,
        "per_request": per_request,
    }


def format_estimate(est: ScanEstimate) -> None:
    sep = "-" * 42
    print(f"\n  {sep}")
    print("  Scan Estimate")
    print(f"  {sep}")
    print(f"    Profile:     {est.profile}")
    print(f"    Model:       {est.model_label}")
    print(f"    Patch model: {est.patch_model_label}")
    print(f"    Files:       {est.file_count} files, {est.total_chars:,} chars")
    print(f"    Chunks:      {est.chunk_count} (+ 1 planning call)")
    print(f"    API calls:   ~{est.total_api_calls_low} -- {est.total_api_calls_high}")
    print(f"    Est. input:  ~{_fmt_tokens(est.input_tokens_low)} -- {_fmt_tokens(est.input_tokens_high)} tokens")
    print(f"    Est. output: ~{_fmt_tokens(est.output_tokens_low)} -- {_fmt_tokens(est.output_tokens_high)} tokens")
    print(f"    Est. cost:   {_fmt_cost(est.cost_min)} -- {_fmt_cost(est.cost_max)}")
    print(f"    Est. time:   ~{_fmt_time(est.time_min)} -- {_fmt_time(est.time_max)}")
    if est.learned_samples:
        print(f"    Learned:     calibrated from {est.learned_samples} past run(s)")
    if not est.is_known_model:
        print("    Warning:     Unknown model -- using conservative estimate")
    print(f"  {sep}\n")


def format_actual_cost(usage) -> str:
    """Format actual cost line using per-model token accounting."""
    thinking_tokens = int(getattr(usage, "usage_metadata_totals", {}).get("thoughtsTokenCount", 0))
    if not getattr(usage, "per_model", None):
        pricing, _ = get_pricing("gemini-2.5-pro")
        actual = (
            usage.prompt_tokens / 1_000_000 * pricing.input_per_m
            + usage.completion_tokens / 1_000_000 * pricing.output_per_m
        )
        actual *= _DISPLAY_COST_MULTIPLIER
    else:
        breakdown = compute_usage_cost_breakdown(usage)
        actual = float(breakdown["display_total_cost_usd"])
    thinking_part = f" + {thinking_tokens:,} thinking" if thinking_tokens else ""
    if str(settings.billing_currency).upper() == "EUR":
        return (
            f"  Actual: {usage.prompt_tokens:,} in + {usage.completion_tokens:,} out"
            f"{thinking_part} = {_fmt_cost(actual)} (~{_fmt_eur(actual)})"
        )
    return f"  Actual: {usage.prompt_tokens:,} in + {usage.completion_tokens:,} out{thinking_part} = {_fmt_cost(actual)}"
