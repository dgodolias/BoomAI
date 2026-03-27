"""Local cost and time estimation for BoomAI scans.

Estimates the two-stage BoomAI pipeline:
1. findings-only scan
2. targeted patch generation for a subset of findings
"""

from __future__ import annotations

import math
from dataclasses import dataclass

CHARS_PER_TOKEN = 3.7
SCAN_OUTPUT_RATIO_LOW = 0.01
SCAN_OUTPUT_RATIO_HIGH = 0.12
PATCH_OUTPUT_RATIO_LOW = 0.15
PATCH_OUTPUT_RATIO_HIGH = 0.45
PLAN_OUTPUT_RATIO = 0.20

_PLAN_TIME_S = 15.0
_PRO_TIME_PER_CALL_S = 120.0
_FLASH_TIME_PER_CALL_S = 30.0
_PRO_PATCH_TIME_PER_CALL_S = 35.0
_FLASH_PATCH_TIME_PER_CALL_S = 12.0


@dataclass(frozen=True)
class ModelPricing:
    input_per_m: float
    output_per_m: float
    label: str


PRICING: list[tuple[str, ModelPricing]] = [
    ("gemini-3-pro-preview", ModelPricing(1.25, 10.00, "Gemini 3 Pro Preview")),
    ("gemini-3-flash-preview", ModelPricing(0.50, 3.00, "Gemini 3 Flash Preview")),
    ("gemini-2.5-pro", ModelPricing(1.25, 10.00, "Gemini 2.5 Pro")),
    ("gemini-2.5-flash", ModelPricing(0.30, 2.50, "Gemini 2.5 Flash")),
]

_UNKNOWN = ModelPricing(1.25, 10.00, "Unknown model")


def get_pricing(model_id: str) -> tuple[ModelPricing, bool]:
    for prefix, pricing in PRICING:
        if model_id.startswith(prefix):
            return pricing, True
    return _UNKNOWN, False


@dataclass
class ScanEstimate:
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


def estimate_scan(
    file_contents: list[tuple[str, str]],
    model: str,
    patch_model: str,
    max_scan_chars: int,
    scan_output_tokens: int,
    plan_output_tokens: int,
    languages: list[str] | None = None,
) -> ScanEstimate:
    """Estimate cost and time for the full two-stage scan."""
    from boomai.review.gemini_review import _chunk_files, _compute_scan_output_tokens
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
    scan_output_low = int(plan_output_tokens * PLAN_OUTPUT_RATIO)
    scan_output_high = int(plan_output_tokens * PLAN_OUTPUT_RATIO)
    for chunk in chunks:
        chunk_chars = sum(len(c) for _, c in chunk)
        scan_input += int((scan_system_chars + chunk_chars + 300) / CHARS_PER_TOKEN)
        chunk_cap = _compute_scan_output_tokens(chunk_chars, len(chunk))
        scan_output_low += int(chunk_cap * SCAN_OUTPUT_RATIO_LOW)
        scan_output_high += int(chunk_cap * SCAN_OUTPUT_RATIO_HIGH)

    patch_calls_low = max(chunk_count, math.ceil(total_chars / 75_000))
    patch_calls_high = max(patch_calls_low, math.ceil(total_chars / 25_000))
    patch_prompt_chars_low = fix_system_chars + 4_500
    patch_prompt_chars_high = fix_system_chars + 10_500
    patch_input_low = int((patch_calls_low * patch_prompt_chars_low) / CHARS_PER_TOKEN)
    patch_input_high = int((patch_calls_high * patch_prompt_chars_high) / CHARS_PER_TOKEN)
    patch_output_cap = min(scan_output_tokens, 4096)
    patch_output_low = int(patch_calls_low * patch_output_cap * PATCH_OUTPUT_RATIO_LOW)
    patch_output_high = int(patch_calls_high * patch_output_cap * PATCH_OUTPUT_RATIO_HIGH)

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
    scan_concurrency = 2 if chunk_count > 20 else 3
    patch_concurrency = 3
    scan_batches = math.ceil(chunk_count / scan_concurrency)
    patch_batches_low = math.ceil(patch_calls_low / patch_concurrency)
    patch_batches_high = math.ceil(patch_calls_high / patch_concurrency)
    time_min = _PLAN_TIME_S + scan_batches * scan_per_call * 0.35 + patch_batches_low * patch_per_call
    time_max = _PLAN_TIME_S + scan_batches * scan_per_call + patch_batches_high * patch_per_call

    return ScanEstimate(
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


def format_estimate(est: ScanEstimate) -> None:
    sep = "-" * 42
    print(f"\n  {sep}")
    print("  Scan Estimate")
    print(f"  {sep}")
    print(f"    Model:       {est.model_label}")
    print(f"    Patch model: {est.patch_model_label}")
    print(f"    Files:       {est.file_count} files, {est.total_chars:,} chars")
    print(f"    Chunks:      {est.chunk_count} (+ 1 planning call)")
    print(f"    API calls:   ~{est.total_api_calls_low} -- {est.total_api_calls_high}")
    print(f"    Est. input:  ~{_fmt_tokens(est.input_tokens_low)} -- {_fmt_tokens(est.input_tokens_high)} tokens")
    print(f"    Est. output: ~{_fmt_tokens(est.output_tokens_low)} -- {_fmt_tokens(est.output_tokens_high)} tokens")
    print(f"    Est. cost:   {_fmt_cost(est.cost_min)} -- {_fmt_cost(est.cost_max)}")
    print(f"    Est. time:   ~{_fmt_time(est.time_min)} -- {_fmt_time(est.time_max)}")
    if not est.is_known_model:
        print("    Warning:     Unknown model -- using conservative estimate")
    print(f"  {sep}\n")


def format_actual_cost(usage) -> str:
    """Format actual cost line using per-model token accounting."""
    actual = 0.0
    if getattr(usage, "per_model", None):
        for model_name, bucket in usage.per_model.items():
            pricing, _ = get_pricing(model_name)
            actual += (
                bucket["prompt_tokens"] / 1_000_000 * pricing.input_per_m
                + bucket["completion_tokens"] / 1_000_000 * pricing.output_per_m
            )
    else:
        pricing, _ = get_pricing("gemini-2.5-pro")
        actual = (
            usage.prompt_tokens / 1_000_000 * pricing.input_per_m
            + usage.completion_tokens / 1_000_000 * pricing.output_per_m
        )
    return f"  Actual: {usage.prompt_tokens:,} in + {usage.completion_tokens:,} out = {_fmt_cost(actual)}"
