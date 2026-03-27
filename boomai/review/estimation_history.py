"""Persistent run history and lightweight regression for scan estimates."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from ..core.models import UsageStats

HISTORY_VERSION = 1
MIN_SAMPLES = 4
RIDGE_LAMBDA = 0.05


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


def _fit_ridge(xs: list[list[float]], ys: list[float]) -> list[float] | None:
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
    for i in range(dim):
        xtx[i][i] += RIDGE_LAMBDA
    return _solve_linear_system(xtx, xty)


def _predict(coeffs: list[float], x: list[float]) -> float:
    return sum(a * b for a, b in zip(coeffs, x))


def _relative_error(actual: float, predicted: float) -> float:
    denom = max(abs(actual), 1e-6)
    return abs(actual - predicted) / denom


def _calc_actual_cost(usage: UsageStats, get_pricing) -> float:
    total = 0.0
    if usage.per_model:
        for model_name, bucket in usage.per_model.items():
            pricing, _ = get_pricing(model_name)
            total += (
                bucket["prompt_tokens"] / 1_000_000.0 * pricing.input_per_m
                + bucket["completion_tokens"] / 1_000_000.0 * pricing.output_per_m
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

    coeffs_cost = _fit_ridge(xs, ys_cost)
    coeffs_time = _fit_ridge(xs, ys_time)
    if coeffs_cost is None or coeffs_time is None:
        return None

    current_x = _feature_vector(features)
    baseline_cost = features.base_cost_mid
    baseline_time = features.base_time_mid
    raw_cost = max(0.01, _predict(coeffs_cost, current_x))
    raw_time = max(5.0, _predict(coeffs_time, current_x))

    blend = min(0.85, max(0.0, (len(xs) - MIN_SAMPLES + 1) / 12.0))
    predicted_cost = baseline_cost * (1.0 - blend) + raw_cost * blend
    predicted_time = baseline_time * (1.0 - blend) + raw_time * blend

    fitted_costs = [_predict(coeffs_cost, x) for x in xs]
    fitted_times = [_predict(coeffs_time, x) for x in xs]
    cost_error = sum(_relative_error(a, p) for a, p in zip(ys_cost, fitted_costs)) / len(xs)
    time_error = sum(_relative_error(a, p) for a, p in zip(ys_time, fitted_times)) / len(xs)
    cost_margin = min(0.9, max(0.18, cost_error * 1.35))
    time_margin = min(1.2, max(0.22, time_error * 1.35))

    return LearnedEstimate(
        cost_min=max(0.01, predicted_cost * (1.0 - cost_margin)),
        cost_max=predicted_cost * (1.0 + cost_margin),
        time_min=max(5.0, predicted_time * (1.0 - time_margin)),
        time_max=predicted_time * (1.0 + time_margin),
        samples=len(xs),
        blended=blend > 0,
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
