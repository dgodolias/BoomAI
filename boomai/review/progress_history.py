"""Learned scan progress timing model for smoother CLI progress feedback."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

HISTORY_VERSION = 1
MIN_SAMPLES = 10


@dataclass
class ChunkProgressFeatures:
    chunk_chars: int
    file_count: int
    split_depth: int
    scan_model_flash: int
    profile_deep: int


def _history_path() -> Path:
    return Path.home() / ".boomai" / "progress_history.json"


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
    payload = {"version": HISTORY_VERSION, "records": records[-1000:]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _feature_vector(features: ChunkProgressFeatures) -> list[float]:
    return [
        1.0,
        features.chunk_chars / 10_000.0,
        features.file_count / 10.0,
        float(features.split_depth),
        float(features.scan_model_flash),
        float(features.profile_deep),
    ]


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


def _fit_ols(xs: list[list[float]], ys: list[float]) -> list[float] | None:
    if not xs or len(xs) != len(ys):
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
    return [sum(inv[i][j] * xty[j] for j in range(dim)) for i in range(dim)]


def _predict(coeffs: list[float], x: list[float]) -> float:
    return sum(a * b for a, b in zip(coeffs, x))


def _heuristic_seconds(features: ChunkProgressFeatures) -> float:
    base = 8.0
    chars_component = features.chunk_chars / 3200.0
    file_component = features.file_count * 1.2
    split_component = features.split_depth * 5.0
    profile_component = 8.0 if features.profile_deep else 0.0
    model_component = -4.0 if features.scan_model_flash else 0.0
    return max(8.0, base + chars_component + file_component + split_component + profile_component + model_component)


def predict_chunk_elapsed_seconds(features: ChunkProgressFeatures) -> tuple[float, int]:
    records = _safe_load()
    if len(records) < MIN_SAMPLES:
        return _heuristic_seconds(features), 0

    xs: list[list[float]] = []
    ys: list[float] = []
    for record in records:
        feat = record.get("features")
        actual = record.get("elapsed_seconds")
        if not isinstance(feat, dict):
            continue
        try:
            feature_obj = ChunkProgressFeatures(**feat)
            elapsed = float(actual)
        except (TypeError, ValueError):
            continue
        xs.append(_feature_vector(feature_obj))
        ys.append(elapsed)

    if len(xs) < MIN_SAMPLES:
        return _heuristic_seconds(features), len(xs)

    coeffs = _fit_ols(xs, ys)
    if coeffs is None:
        return _heuristic_seconds(features), len(xs)

    predicted = _predict(coeffs, _feature_vector(features))
    return max(8.0, predicted), len(xs)


def record_chunk_elapsed(features: ChunkProgressFeatures, elapsed_seconds: float) -> None:
    records = _safe_load()
    records.append(
        {
            "features": asdict(features),
            "elapsed_seconds": max(0.1, float(elapsed_seconds)),
        }
    )
    _safe_save(records)
