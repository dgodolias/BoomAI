from __future__ import annotations

from ...core.config import settings
from ...core.google_pricing import effective_rates, get_pricing
from ...core.policies import build_estimate_policy


def fmt_cost(value: float) -> str:
    if value < 0.01:
        return "< $0.01"
    return f"${value:.2f}"


def fmt_eur(value_usd: float) -> str:
    eur = value_usd * float(settings.usd_to_eur_rate)
    if eur < 0.01:
        return "< €0.01"
    return f"€{eur:.2f}"


def event_cost(event: dict[str, object]) -> dict[str, float]:
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
        cost = event_cost(event)
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
        cost = event_cost(event)
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
    display_multiplier = build_estimate_policy().display_cost_multiplier
    displayed_cost = raw_cost * display_multiplier
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0)),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0)),
        "api_calls": int(getattr(usage, "api_calls", 0)),
        "usage_metadata_totals": dict(getattr(usage, "usage_metadata_totals", {})),
        "raw_input_cost_usd": total_input_cost,
        "raw_cached_input_cost_usd": total_cached_input_cost,
        "raw_output_cost_usd": total_output_cost,
        "raw_total_cost_usd": raw_cost,
        "display_multiplier": display_multiplier,
        "display_total_cost_usd": displayed_cost,
        "per_model": per_model,
        "per_stage": per_stage,
        "per_request": per_request,
    }


def format_actual_cost(usage) -> str:
    """Format actual cost line using per-model token accounting."""
    thinking_tokens = int(getattr(usage, "usage_metadata_totals", {}).get("thoughtsTokenCount", 0))
    if not getattr(usage, "per_model", None):
        pricing, _ = get_pricing("gemini-2.5-pro")
        actual = (
            usage.prompt_tokens / 1_000_000 * pricing.input_per_m
            + usage.completion_tokens / 1_000_000 * pricing.output_per_m
        )
        actual *= build_estimate_policy().display_cost_multiplier
    else:
        breakdown = compute_usage_cost_breakdown(usage)
        actual = float(breakdown["display_total_cost_usd"])

    thinking_part = f" + {thinking_tokens:,} thinking" if thinking_tokens else ""
    if str(settings.billing_currency).upper() == "EUR":
        return (
            f"  Actual: {usage.prompt_tokens:,} in + {usage.completion_tokens:,} out"
            f"{thinking_part} = {fmt_cost(actual)} (~{fmt_eur(actual)})"
        )
    return (
        f"  Actual: {usage.prompt_tokens:,} in + {usage.completion_tokens:,} out"
        f"{thinking_part} = {fmt_cost(actual)}"
    )
