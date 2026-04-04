from __future__ import annotations

from ..core.config import settings
from ..core.policies import (
    build_patch_policy,
    build_retry_policy,
    build_scan_policy,
)


def compute_effective_timeout(chunk_chars: int, file_count: int) -> float:
    """Scale chunk timeout with payload size instead of file count alone."""
    scan_policy = build_scan_policy()
    timeout = scan_policy.timeout_seconds
    if file_count == 1:
        timeout *= 2
    if chunk_chars >= 100_000:
        timeout = max(timeout, scan_policy.timeout_seconds + 180)
    elif chunk_chars >= 60_000:
        timeout = max(timeout, scan_policy.timeout_seconds + 90)
    return timeout


def compute_scan_output_tokens(
    chunk_chars: int,
    file_count: int,
    issue_seed_count: int = 0,
) -> int:
    """Choose a tighter output cap based on chunk complexity."""
    scan_policy = build_scan_policy()
    cap = 4096
    if chunk_chars >= 50_000 or file_count >= 6:
        cap = 8192
    if chunk_chars >= 100_000 or file_count >= 10 or issue_seed_count >= 8:
        cap = 16384
    if chunk_chars >= 200_000 or file_count >= 20 or issue_seed_count >= 16:
        cap = 24576
    if chunk_chars >= 320_000 or file_count >= 35 or issue_seed_count >= 24:
        cap = 32768
    return min(scan_policy.output_tokens, cap)


def effective_scan_char_budget(char_budget: int) -> int:
    """Reserve room for prompt wrappers, issue seeds, and related snippets."""
    scan_policy = build_scan_policy()
    return max(40_000, char_budget - scan_policy.chunk_reserved_chars)


def compute_scan_concurrency(model_name: str, chunk_count: int) -> int:
    """Choose safer concurrency for pro models and higher throughput for flash."""
    lowered = model_name.lower()
    scan_policy = build_scan_policy()
    if "flash" in lowered:
        limit = scan_policy.flash_max_concurrency
        if chunk_count >= 12:
            limit = min(limit, 3)
        return max(1, limit)

    limit = scan_policy.pro_max_concurrency
    if chunk_count <= 3:
        limit = min(limit + 1, scan_policy.flash_max_concurrency)
    return max(1, limit)


def compute_patch_concurrency(model_name: str, patch_count: int) -> int:
    """Use more parallelism for flash patch generation, less for pro."""
    lowered = model_name.lower()
    patch_policy = build_patch_policy()
    limit = patch_policy.max_concurrency
    if "flash" not in lowered:
        limit = min(limit, 2)
    if patch_count <= 2:
        limit = min(limit, patch_count)
    return max(1, limit)


def is_gemini3_family(model_name: str) -> bool:
    lowered = model_name.lower()
    return "gemini-3" in lowered or "gemini-3.1" in lowered


def is_flash_model(model_name: str) -> bool:
    return "flash" in model_name.lower()


def is_pro_model(model_name: str) -> bool:
    lowered = model_name.lower()
    return "pro" in lowered and "flash" not in lowered


def normalize_scan_output_tokens(model_name: str, requested_output_tokens: int) -> int:
    """Leave enough output headroom for Gemini 3 Pro structured JSON."""
    scan_policy = build_scan_policy()
    effective = max(1, int(requested_output_tokens))
    if is_gemini3_family(model_name) and is_pro_model(model_name):
        effective = max(effective, scan_policy.pro_min_output_tokens)
    return min(scan_policy.output_tokens, effective)


def build_generation_config(
    model_name: str,
    max_output_tokens: int,
    response_json_schema: dict | None = None,
) -> dict:
    """Build generation config with model-specific stability controls."""
    config = {
        "maxOutputTokens": max_output_tokens,
        "temperature": 0.1,
        "responseMimeType": "application/json",
    }
    if response_json_schema is not None:
        config["responseJsonSchema"] = response_json_schema

    lowered = model_name.lower()
    if is_gemini3_family(model_name):
        if is_flash_model(model_name):
            config["thinkingConfig"] = {"thinkingLevel": settings.gemini3_flash_thinking_level}
        elif is_pro_model(model_name):
            config["thinkingConfig"] = {"thinkingLevel": settings.gemini3_pro_thinking_level}
    elif "flash" in lowered:
        config["thinkingConfig"] = {"thinkingBudget": 0}

    return config


def get_heartbeat_interval_seconds() -> int:
    return build_retry_policy().heartbeat_interval_seconds


def get_http_max_retries() -> int:
    return build_retry_policy().http_max_retries
