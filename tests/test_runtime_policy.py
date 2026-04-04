from __future__ import annotations

from boomai.review.runtime_policy import (
    compute_patch_concurrency,
    compute_scan_concurrency,
    compute_scan_output_tokens,
    normalize_scan_output_tokens,
)


def test_scan_output_tokens_scale_with_chunk_size() -> None:
    assert compute_scan_output_tokens(5_000, 1) == 4096
    assert compute_scan_output_tokens(120_000, 12) >= 16384


def test_normalize_scan_output_tokens_respects_pro_floor() -> None:
    assert normalize_scan_output_tokens("gemini-3.1-pro-preview", 4096) >= 8192
    assert normalize_scan_output_tokens("gemini-3.1-flash-lite-preview", 4096) == 4096


def test_concurrency_policies_follow_model_family() -> None:
    assert compute_patch_concurrency("gemini-3.1-pro-preview", 10) <= 2
    assert compute_scan_concurrency("gemini-3.1-flash-lite-preview", 10) >= 1
