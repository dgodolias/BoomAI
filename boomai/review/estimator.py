"""Local cost and time estimation for BoomAI scans.

Computes estimates without any API calls — all local math using
character-to-token heuristics and a model pricing table.
Shows a min-max cost range based on output token uncertainty.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ── Constants ─────────────────────────────────────────────────
CHARS_PER_TOKEN = 3.7           # code averages ~3.7 chars/token (Gemini BPE)
OUTPUT_RATIO_LOW = 0.02         # optimistic: clean code, few findings
OUTPUT_RATIO_HIGH = 0.50        # pessimistic: many findings, big JSON
PLAN_OUTPUT_RATIO = 0.20        # planning response is short

# ── Time estimation ──────────────────────────────────────────
_PLAN_TIME_S = 15.0
_PRO_TIME_PER_CALL_S = 120.0   # observed ~150s, use 120 avg
_FLASH_TIME_PER_CALL_S = 30.0


# ── Pricing table ────────────────────────────────────────────
# Prefix-matched against model ID. Most-specific first.
# Prices: USD per 1M tokens (input, output) for <=200K context.

@dataclass(frozen=True)
class ModelPricing:
    input_per_m: float
    output_per_m: float
    label: str


PRICING: list[tuple[str, ModelPricing]] = [
    ("gemini-3.1-pro", ModelPricing(1.25, 10.00, "Gemini 3.1 Pro")),
    ("gemini-3-flash",  ModelPricing(0.50,  3.00, "Gemini 3 Flash")),
    ("gemini-2.5-pro",  ModelPricing(1.25, 10.00, "Gemini 2.5 Pro")),
    ("gemini-2.5-flash", ModelPricing(0.30,  2.50, "Gemini 2.5 Flash")),
]

_UNKNOWN = ModelPricing(1.25, 10.00, "Unknown model")


def get_pricing(model_id: str) -> tuple[ModelPricing, bool]:
    """Return (pricing, is_known). is_known=False -> used fallback."""
    for prefix, pricing in PRICING:
        if model_id.startswith(prefix):
            return pricing, True
    return _UNKNOWN, False


# ── Estimate result ──────────────────────────────────────────

@dataclass
class ScanEstimate:
    model: str
    model_label: str
    is_known_model: bool
    file_count: int
    total_chars: int
    chunk_count: int
    total_api_calls: int
    input_tokens: int
    output_tokens_low: int
    output_tokens_high: int
    cost_min: float
    cost_max: float
    time_min: float
    time_max: float


# ── Core estimation ──────────────────────────────────────────

def estimate_scan(
    file_contents: list[tuple[str, str]],
    model: str,
    max_scan_chars: int,
    scan_output_tokens: int,
    plan_output_tokens: int,
    languages: list[str] | None = None,
) -> ScanEstimate:
    """Estimate cost and time for a full codebase scan (no API calls)."""
    from boomai.review.gemini_review import _chunk_files
    from boomai.review.prompts import build_plan_prompt, build_scan_system_prompt

    total_chars = sum(len(c) for _, c in file_contents)
    chunks = _chunk_files(file_contents, max_scan_chars)
    chunk_count = len(chunks)

    # Dynamic prompt sizing — measure actual prompts instead of hardcoding
    langs = languages or []
    scan_system_chars = len(build_scan_system_prompt(langs))
    plan_system_chars = len(build_plan_prompt(max_scan_chars))

    # Input tokens (deterministic — same regardless of output)
    plan_input = int(plan_system_chars / CHARS_PER_TOKEN)
    scan_input = 0
    for chunk in chunks:
        chunk_chars = sum(len(c) for _, c in chunk)
        per_chunk = scan_system_chars + chunk_chars + 300  # user msg overhead
        scan_input += int(per_chunk / CHARS_PER_TOKEN)
    total_input = plan_input + scan_input

    # Output tokens — range
    plan_output = int(plan_output_tokens * PLAN_OUTPUT_RATIO)
    output_low = plan_output + chunk_count * int(scan_output_tokens * OUTPUT_RATIO_LOW)
    output_high = plan_output + chunk_count * int(scan_output_tokens * OUTPUT_RATIO_HIGH)

    # Cost range
    pricing, is_known = get_pricing(model)
    input_cost = total_input / 1_000_000 * pricing.input_per_m
    cost_min = input_cost + output_low / 1_000_000 * pricing.output_per_m
    cost_max = input_cost + output_high / 1_000_000 * pricing.output_per_m

    # Time range — less output = faster response
    is_flash = "flash" in model.lower()
    per_call = _FLASH_TIME_PER_CALL_S if is_flash else _PRO_TIME_PER_CALL_S
    concurrency = 2 if chunk_count > 20 else 3
    batches = math.ceil(chunk_count / concurrency)
    time_min = _PLAN_TIME_S + batches * per_call * 0.3   # few findings → fast
    time_max = _PLAN_TIME_S + batches * per_call          # many findings → slow

    return ScanEstimate(
        model=model,
        model_label=pricing.label,
        is_known_model=is_known,
        file_count=len(file_contents),
        total_chars=total_chars,
        chunk_count=chunk_count,
        total_api_calls=1 + chunk_count,
        input_tokens=total_input,
        output_tokens_low=output_low,
        output_tokens_high=output_high,
        cost_min=cost_min,
        cost_max=cost_max,
        time_min=time_min,
        time_max=time_max,
    )


# ── Display ──────────────────────────────────────────────────

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
    """Print scan estimate to stdout."""
    sep = "-" * 42
    print(f"\n  {sep}")
    print(f"  Scan Estimate")
    print(f"  {sep}")
    print(f"    Model:       {est.model_label}")
    print(f"    Files:       {est.file_count} files, {est.total_chars:,} chars")
    print(f"    Chunks:      {est.chunk_count} (+ 1 planning call)")
    print(f"    Est. input:  ~{_fmt_tokens(est.input_tokens)} tokens")
    print(f"    Est. output: ~{_fmt_tokens(est.output_tokens_low)} -- {_fmt_tokens(est.output_tokens_high)} tokens")
    print(f"    Est. cost:   {_fmt_cost(est.cost_min)} -- {_fmt_cost(est.cost_max)}")
    print(f"    Est. time:   ~{_fmt_time(est.time_min)} -- {_fmt_time(est.time_max)}")

    if not est.is_known_model:
        print(f"    Warning:     Unknown model -- using conservative estimate")

    print(f"  {sep}\n")


def format_actual_cost(prompt_tokens: int, completion_tokens: int,
                       model: str) -> str:
    """Format actual cost line for post-scan display."""
    pricing, _ = get_pricing(model)
    actual = (prompt_tokens / 1_000_000 * pricing.input_per_m
              + completion_tokens / 1_000_000 * pricing.output_per_m)
    return (f"  Actual: {prompt_tokens:,} in + {completion_tokens:,} out "
            f"= {_fmt_cost(actual)}")
