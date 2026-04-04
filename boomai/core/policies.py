from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .config import settings


class ScanPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    timeout_seconds: float
    output_tokens: int
    pro_min_output_tokens: int
    chunk_reserved_chars: int
    pro_max_concurrency: int
    flash_max_concurrency: int


class PatchPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_concurrency: int


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    http_max_retries: int = 3
    heartbeat_interval_seconds: int = 10


class EstimatePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    chars_per_token: float = 3.7
    patch_output_ratio_low: float = 0.05
    patch_output_ratio_high: float = 0.18
    plan_time_seconds: float = 15.0
    pro_time_per_call_seconds: float = 120.0
    flash_time_per_call_seconds: float = 30.0
    pro_patch_time_per_call_seconds: float = 35.0
    flash_patch_time_per_call_seconds: float = 12.0
    display_cost_multiplier: float = 1.0


class ProgressPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    bar_width: int = 28
    spinner_frames: str = "|/-\\"


class CatalogPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_cache_ttl_hours: int
    pricing_cache_ttl_hours: int


class RetrievalPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_issue_seeds: int = 12
    max_snippets: int = 10
    max_snippet_chars: int = 12_000
    snippet_radius_lines: int = 18


class StaticAnalysisPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_issue_seeds: int = 40
    initial_file_cap: int = 2
    initial_rule_family_cap: int = 3
    relaxed_file_cap: int = 4
    relaxed_rule_family_cap: int = 6


def build_scan_policy() -> ScanPolicy:
    return ScanPolicy(
        timeout_seconds=settings.scan_timeout,
        output_tokens=settings.scan_output_tokens,
        pro_min_output_tokens=settings.scan_pro_min_output_tokens,
        chunk_reserved_chars=settings.scan_chunk_reserved_chars,
        pro_max_concurrency=settings.scan_pro_max_concurrency,
        flash_max_concurrency=settings.scan_flash_max_concurrency,
    )


def build_patch_policy() -> PatchPolicy:
    return PatchPolicy(max_concurrency=settings.patch_max_concurrency)


def build_retry_policy() -> RetryPolicy:
    return RetryPolicy()


def build_estimate_policy() -> EstimatePolicy:
    return EstimatePolicy()


def build_progress_policy() -> ProgressPolicy:
    return ProgressPolicy()


def build_catalog_policy() -> CatalogPolicy:
    return CatalogPolicy(
        model_cache_ttl_hours=int(settings.model_catalog_cache_ttl_hours or 24),
        pricing_cache_ttl_hours=int(settings.pricing_catalog_cache_ttl_hours or 24),
    )


def build_retrieval_policy() -> RetrievalPolicy:
    return RetrievalPolicy()


def build_static_analysis_policy() -> StaticAnalysisPolicy:
    return StaticAnalysisPolicy()
