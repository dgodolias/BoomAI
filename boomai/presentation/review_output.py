from __future__ import annotations

from ..core.models import ReviewSummary
from ..review.services.cost_attribution import format_actual_cost


def format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def print_review(
    review: ReviewSummary,
    applied: int = 0,
    elapsed: float = 0,
    *,
    show_usage: bool = True,
) -> None:
    fixable = sum(1 for finding in review.findings if finding.suggestion and finding.old_code)
    non_fixable = len(review.findings) - fixable
    print(f"\n  {'=' * 56}")
    parts = [f"BoomAI Review — {len(review.findings)} issues"]
    if applied:
        parts.append(f"{applied} fixes applied")
    if elapsed:
        parts.append(format_elapsed(elapsed))
    print(f"  {' | '.join(parts)}")
    if show_usage and review.usage and review.usage.api_calls > 0:
        print(format_actual_cost(review.usage))
        print(
            f"  Stats: {review.usage.api_calls} API calls | "
            f"{review.usage.prompt_tokens:,} input | {review.usage.completion_tokens:,} output"
        )
        if len(review.usage.per_model) > 1:
            mixes = ", ".join(
                f"{model} x{bucket['api_calls']}"
                for model, bucket in review.usage.per_model.items()
            )
            print(f"  Models: {mixes}")
    print(f"  Findings: {fixable} fixable | {non_fixable} non-fixable")
    print(f"  {'=' * 56}")
    print(f"\n  {review.summary}\n")

    if not review.findings:
        print("  No issues found!")
        return

    for index, finding in enumerate(review.findings, 1):
        has_fix = " [FIX]" if finding.suggestion else ""
        print(f"  #{index} {finding.file}:{finding.line}{has_fix}")
        for line in finding.body.split("\n")[:3]:
            print(f"      {line}")
        print()
