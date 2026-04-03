"""Persistent run history and lightweight regression for scan estimates."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from ..core.models import UsageStats

HISTORY_VERSION = 1
MIN_SAMPLES = 8
Z_95 = 1.96


@dataclass
class EstimateFeatures:
    total_chars: int
    file_count: int
    chunk_count: int
    api_calls_mid: float
    input_tokens_mid: float
    output_tokens_mid: float
    base_cost_mid: float
    base_time_mid: float
    scan_model_flash: int
    patch_model_flash: int
    scan_profile_deep: int = 0


@dataclass
class LearnedEstimate:
    cost_min: float
    cost_max: float
    time_min: float
    time_max: float
    samples: int
    blended: bool


def _history_path() -> Path:
    return Path.home() / ".boomai" / "estimation_history.json"


def _safe_load() -> list[dict]:
    path = _history_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("version") != HISTORY_VERSION:
        return []
    records = payload.get("records", [])
    return records if isinstance(records, list) else []


def _safe_save(records: list[dict]) -> None:
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": HISTORY_VERSION, "records": records[-200:]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _feature_vector(features: EstimateFeatures) -> list[float]:
    return [
        1.0,
        features.total_chars / 100_000.0,
        features.file_count / 100.0,
        float(features.chunk_count),
        features.api_calls_mid / 10.0,
        features.input_tokens_mid / 100_000.0,
        features.output_tokens_mid / 10_000.0,
        features.base_cost_mid,
        features.base_time_mid / 60.0,
        float(features.scan_model_flash),
        float(features.patch_model_flash),
        float(features.scan_profile_deep),
    ]


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(vector)
    augmented = [row[:] + [vector[i]] for i, row in enumerate(matrix)]

    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(augmented[r][col]))
        if abs(augmented[pivot][col]) < 1e-9:
            return None
        if pivot != col:
            augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        pivot_value = augmented[col][col]
        for j in range(col, n + 1):
            augmented[col][j] /= pivot_value
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col]
            if factor == 0:
                continue
            for j in range(col, n + 1):
                augmented[row][j] -= factor * augmented[col][j]

    return [augmented[i][n] for i in range(n)]


def _matrix_inverse(matrix: list[list[float]]) -> list[list[float]] | None:
    n = len(matrix)
    augmented = [
        matrix[i][:] + [1.0 if i == j else 0.0 for j in range(n)]
        for i in range(n)
    ]

    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(augmented[r][col]))
        if abs(augmented[pivot][col]) < 1e-9:
            return None
        if pivot != col:
            augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        pivot_value = augmented[col][col]
        for j in range(2 * n):
            augmented[col][j] /= pivot_value
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col]
            if factor == 0:
                continue
            for j in range(2 * n):
                augmented[row][j] -= factor * augmented[col][j]

    return [row[n:] for row in augmented]


def _fit_ols(xs: list[list[float]], ys: list[float]) -> tuple[list[float], list[list[float]]] | None:
    if not xs or not ys or len(xs) != len(ys):
        return None
    dim = len(xs[0])
    xtx = [[0.0 for _ in range(dim)] for _ in range(dim)]
    xty = [0.0 for _ in range(dim)]
    for x, y in zip(xs, ys):
        for i in range(dim):
            xty[i] += x[i] * y
            for j in range(dim):
                xtx[i][j] += x[i] * x[j]
    inv = _matrix_inverse(xtx)
    if inv is None:
        return None
    coeffs = [sum(inv[i][j] * xty[j] for j in range(dim)) for i in range(dim)]
    return coeffs, inv


def _predict(coeffs: list[float], x: list[float]) -> float:
    return sum(a * b for a, b in zip(coeffs, x))


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _matvec(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [sum(row[j] * vector[j] for j in range(len(vector))) for row in matrix]


def _prediction_interval(
    xs: list[list[float]],
    ys: list[float],
    coeffs: list[float],
    inv_xtx: list[list[float]],
    current_x: list[float],
    floor: float,
) -> tuple[float, float]:
    n = len(xs)
    p = len(current_x)
    predicted = max(floor, _predict(coeffs, current_x))
    if n <= p:
        margin = max(predicted * 0.25, floor)
        return max(floor, predicted - margin), predicted + margin

    residuals = [actual - _predict(coeffs, x) for x, actual in zip(xs, ys)]
    sse = sum(r * r for r in residuals)
    dof = max(1, n - p)
    sigma2 = max(1e-9, sse / dof)
    leverage = max(0.0, _dot(current_x, _matvec(inv_xtx, current_x)))
    std_pred = math.sqrt(sigma2 * (1.0 + leverage))
    margin = Z_95 * std_pred
    return max(floor, predicted - margin), predicted + margin


def _calc_actual_cost(usage: UsageStats, get_pricing) -> float:
    if getattr(usage, "request_events", None):
        total = 0.0
        for event in usage.request_events:
            if not isinstance(event, dict):
                continue
            model_name = str(event.get("model", "") or "")
            pricing, _ = get_pricing(model_name)
            usage_meta = event.get("usage_metadata", {}) or {}
            if not isinstance(usage_meta, dict):
                usage_meta = {}
            prompt_tokens = int(event.get("prompt_tokens", 0) or 0)
            completion_tokens = int(event.get("completion_tokens", 0) or 0)
            cached_tokens = int(usage_meta.get("cachedContentTokenCount", 0) or 0)
            thinking_tokens = int(usage_meta.get("thoughtsTokenCount", 0) or 0)
            use_high = prompt_tokens > 200_000
            input_rate = (
                pricing.input_per_m_high
                if use_high and getattr(pricing, "input_per_m_high", None) is not None
                else pricing.input_per_m
            )
            output_rate = (
                pricing.output_per_m_high
                if use_high and getattr(pricing, "output_per_m_high", None) is not None
                else pricing.output_per_m
            )
            cached_rate = (
                pricing.cached_input_per_m_high
                if use_high and getattr(pricing, "cached_input_per_m_high", None) is not None
                else getattr(pricing, "cached_input_per_m", None)
            )
            if cached_rate is None:
                cached_rate = input_rate
            noncached_prompt = max(0, prompt_tokens - cached_tokens)
            billed_output = completion_tokens + thinking_tokens
            total += (
                noncached_prompt / 1_000_000.0 * input_rate
                + cached_tokens / 1_000_000.0 * cached_rate
                + billed_output / 1_000_000.0 * output_rate
            )
        return total

    pricing, _ = get_pricing("gemini-2.5-pro")
    return (
        usage.prompt_tokens / 1_000_000.0 * pricing.input_per_m
        + usage.completion_tokens / 1_000_000.0 * pricing.output_per_m
    )


def learn_adjustment(features: EstimateFeatures) -> LearnedEstimate | None:
    records = _safe_load()
    if len(records) < MIN_SAMPLES:
        return None

    xs = []
    ys_cost = []
    ys_time = []
    for record in records:
        feat = record.get("features")
        actual = record.get("actual")
        if not isinstance(feat, dict) or not isinstance(actual, dict):
            continue
        try:
            feature_obj = EstimateFeatures(**feat)
            actual_cost = float(actual["cost"])
            actual_time = float(actual["time_seconds"])
        except (TypeError, ValueError, KeyError):
            continue
        xs.append(_feature_vector(feature_obj))
        ys_cost.append(actual_cost)
        ys_time.append(actual_time)

    if len(xs) < MIN_SAMPLES:
        return None

    cost_fit = _fit_ols(xs, ys_cost)
    time_fit = _fit_ols(xs, ys_time)
    if cost_fit is None or time_fit is None:
        return None

    coeffs_cost, cost_inv = cost_fit
    coeffs_time, time_inv = time_fit
    current_x = _feature_vector(features)
    cost_min, cost_max = _prediction_interval(xs, ys_cost, coeffs_cost, cost_inv, current_x, 0.01)
    time_min, time_max = _prediction_interval(xs, ys_time, coeffs_time, time_inv, current_x, 5.0)

    return LearnedEstimate(
        cost_min=cost_min,
        cost_max=cost_max,
        time_min=time_min,
        time_max=time_max,
        samples=len(xs),
        blended=False,
    )


def record_run(
    features: EstimateFeatures,
    elapsed_seconds: float,
    usage: UsageStats,
    findings_count: int,
    applied_count: int,
    get_pricing,
) -> None:
    records = _safe_load()
    records.append(
        {
            "features": asdict(features),
            "actual": {
                "time_seconds": elapsed_seconds,
                "cost": _calc_actual_cost(usage, get_pricing),
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "api_calls": usage.api_calls,
                "findings_count": findings_count,
                "applied_count": applied_count,
            },
        }
    )
    _safe_save(records)
