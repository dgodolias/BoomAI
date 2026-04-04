from __future__ import annotations


def fmt_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value // 1_000}K"
    return str(value)


def fmt_time(seconds: float) -> str:
    total_seconds = int(seconds)
    if total_seconds < 60:
        return f"{total_seconds}s"
    return f"{total_seconds // 60}m {total_seconds % 60:02d}s"


def fmt_cost(value: float) -> str:
    if value < 0.01:
        return "< $0.01"
    return f"${value:.2f}"


def format_estimate(est) -> None:
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
    print(f"    Est. input:  ~{fmt_tokens(est.input_tokens_low)} -- {fmt_tokens(est.input_tokens_high)} tokens")
    print(f"    Est. output: ~{fmt_tokens(est.output_tokens_low)} -- {fmt_tokens(est.output_tokens_high)} tokens")
    print(f"    Est. cost:   {fmt_cost(est.cost_min)} -- {fmt_cost(est.cost_max)}")
    print(f"    Est. time:   ~{fmt_time(est.time_min)} -- {fmt_time(est.time_max)}")
    if est.learned_samples:
        learned_label = "lightly calibrated" if getattr(est, "learned_blended", False) else "calibrated"
        print(f"    Learned:     {learned_label} from {est.learned_samples} past run(s)")
    else:
        remaining = max(0, 3 - int(getattr(est, "recorded_samples", 0)))
        if remaining > 0:
            print(
                f"    Learned:     waiting for {remaining} more recorded run(s) "
                f"(have {getattr(est, 'recorded_samples', 0)})"
            )
    if not est.is_known_model:
        print("    Warning:     Unknown model -- using conservative estimate")
    print(f"  {sep}\n")
