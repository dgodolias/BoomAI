from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings

_CATALOG_SCHEMA_VERSION = 1
_MODEL_CACHE_PATH = Path.home() / ".boomai" / "model_catalog.json"
_MODELS_API_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200"

_LEGACY_DEFAULT_STRONG_MODEL = "gemini-3.1-pro-preview"
_LEGACY_DEFAULT_WEAK_MODEL = "gemini-3.1-flash-lite-preview"
_FALLBACK_STRONG_ALIAS = "gemini-pro-latest"
_FALLBACK_WEAK_ALIAS = "gemini-flash-lite-latest"
_FALLBACK_WEAK_SECONDARY_ALIAS = "gemini-flash-latest"
_EXCLUDED_NAME_TOKENS = (
    "customtools",
    "embedding",
    "image",
    "tts",
    "robotics",
    "computer-use",
    "native-audio",
    "live-preview",
)
_VERSION_RE = re.compile(r"gemini-(\d+(?:\.\d+)?)")
_DATE_RE = re.compile(r"(?P<month>\d{2})-(?P<year>20\d{2})")
_REVISION_RE = re.compile(r"-(\d{3})(?:$|[^0-9])")


@dataclass(frozen=True)
class GoogleModelEntry:
    model_id: str
    api_name: str
    display_name: str
    version: str
    family: str
    is_alias: bool
    input_token_limit: int = 0
    output_token_limit: int = 0
    supported_generation_methods: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeModels:
    strong_model_id: str
    weak_model_id: str
    strong_display_name: str
    weak_display_name: str
    strong_mode: str
    weak_mode: str
    source: str
    fetched_at_utc: str | None
    catalog: tuple[GoogleModelEntry, ...]
    strong_candidates: tuple[GoogleModelEntry, ...]
    weak_candidates: tuple[GoogleModelEntry, ...]
    strong_fallback_models: tuple[str, ...]
    weak_fallback_models: tuple[str, ...]
    catalog_error: str | None = None


def normalize_model_id(value: str) -> str:
    model_id = (value or "").strip()
    if model_id.startswith("models/"):
        model_id = model_id[len("models/"):]
    return model_id


def build_generate_content_url(model_id: str) -> str:
    normalized = normalize_model_id(model_id)
    return (
        f"{settings.gemini_base_url}/{normalized}"
        f":generateContent?key={settings.google_api_key}"
    )


def _is_alias_model(model_id: str) -> bool:
    lowered = model_id.lower()
    return lowered.endswith("-latest") or "latest" in lowered


def _classify_family(model_id: str) -> str:
    lowered = model_id.lower()
    if "flash-lite" in lowered:
        return "flash-lite"
    if "flash" in lowered:
        return "flash"
    if "pro" in lowered:
        return "pro"
    return "other"


def _is_eligible_model(raw_model: dict[str, object]) -> bool:
    api_name = str(raw_model.get("name", "") or "")
    lowered = api_name.lower()
    if not lowered.startswith("models/gemini"):
        return False
    methods = raw_model.get("supportedGenerationMethods") or []
    if not isinstance(methods, list) or "generateContent" not in methods:
        return False
    if any(token in lowered for token in _EXCLUDED_NAME_TOKENS):
        return False
    return True


def _normalize_entry(raw_model: dict[str, object]) -> GoogleModelEntry | None:
    if not _is_eligible_model(raw_model):
        return None
    api_name = str(raw_model.get("name", "") or "")
    model_id = normalize_model_id(api_name)
    methods = raw_model.get("supportedGenerationMethods") or []
    normalized_methods = tuple(
        str(method)
        for method in methods
        if isinstance(method, str)
    )
    return GoogleModelEntry(
        model_id=model_id,
        api_name=api_name,
        display_name=str(raw_model.get("displayName", model_id) or model_id),
        version=str(raw_model.get("version", "") or ""),
        family=_classify_family(model_id),
        is_alias=_is_alias_model(model_id),
        input_token_limit=int(raw_model.get("inputTokenLimit", 0) or 0),
        output_token_limit=int(raw_model.get("outputTokenLimit", 0) or 0),
        supported_generation_methods=normalized_methods,
    )


def _parse_generation(model_id: str) -> float:
    match = _VERSION_RE.search(model_id.lower())
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def _parse_date_rank(entry: GoogleModelEntry) -> tuple[int, int]:
    haystack = f"{entry.version} {entry.model_id}"
    match = _DATE_RE.search(haystack)
    if not match:
        return (0, 0)
    month = int(match.group("month"))
    year = int(match.group("year"))
    return (year, month)


def _parse_revision_rank(entry: GoogleModelEntry) -> int:
    haystack = f"{entry.version} {entry.model_id}"
    match = _REVISION_RE.search(haystack)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def _entry_sort_key(entry: GoogleModelEntry) -> tuple[float, int, int, int, int]:
    lowered = entry.model_id.lower()
    stable_rank = 1 if "preview" not in lowered else 0
    alias_rank = 0 if entry.is_alias else 1
    year, month = _parse_date_rank(entry)
    revision = _parse_revision_rank(entry)
    return (
        _parse_generation(entry.model_id),
        year,
        month,
        stable_rank,
        revision * 10 + alias_rank,
    )


def _dedupe_models(models: Iterable[GoogleModelEntry]) -> list[GoogleModelEntry]:
    deduped: dict[str, GoogleModelEntry] = {}
    for entry in models:
        existing = deduped.get(entry.model_id)
        if existing is None or _entry_sort_key(entry) > _entry_sort_key(existing):
            deduped[entry.model_id] = entry
    return list(deduped.values())


def _sort_models_desc(models: Iterable[GoogleModelEntry]) -> list[GoogleModelEntry]:
    return sorted(_dedupe_models(models), key=_entry_sort_key, reverse=True)


def _built_in_catalog() -> list[GoogleModelEntry]:
    return [
        GoogleModelEntry(
            model_id=_FALLBACK_STRONG_ALIAS,
            api_name=f"models/{_FALLBACK_STRONG_ALIAS}",
            display_name="Gemini Pro Latest",
            version="Gemini Pro Latest",
            family="pro",
            is_alias=True,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supported_generation_methods=("generateContent", "countTokens", "createCachedContent"),
        ),
        GoogleModelEntry(
            model_id=_FALLBACK_WEAK_ALIAS,
            api_name=f"models/{_FALLBACK_WEAK_ALIAS}",
            display_name="Gemini Flash-Lite Latest",
            version="Gemini Flash-Lite Latest",
            family="flash-lite",
            is_alias=True,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supported_generation_methods=("generateContent", "countTokens", "createCachedContent"),
        ),
        GoogleModelEntry(
            model_id=_FALLBACK_WEAK_SECONDARY_ALIAS,
            api_name=f"models/{_FALLBACK_WEAK_SECONDARY_ALIAS}",
            display_name="Gemini Flash Latest",
            version="Gemini Flash Latest",
            family="flash",
            is_alias=True,
            input_token_limit=1_048_576,
            output_token_limit=65_536,
            supported_generation_methods=("generateContent", "countTokens", "createCachedContent"),
        ),
    ]


def _load_catalog_cache() -> dict[str, object] | None:
    if not _MODEL_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(_MODEL_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if int(data.get("schema_version", 0) or 0) != _CATALOG_SCHEMA_VERSION:
        return None
    return data


def _save_catalog_cache(
    models: list[GoogleModelEntry],
    *,
    fetched_at_utc: str,
    auto_strong_model_id: str,
    auto_weak_model_id: str,
) -> None:
    _MODEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _CATALOG_SCHEMA_VERSION,
        "fetched_at_utc": fetched_at_utc,
        "auto_strong_model_id": auto_strong_model_id,
        "auto_weak_model_id": auto_weak_model_id,
        "models": [asdict(model) for model in models],
    }
    _MODEL_CACHE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _deserialize_models(raw_models: Iterable[dict[str, object]]) -> list[GoogleModelEntry]:
    models: list[GoogleModelEntry] = []
    for raw_model in raw_models:
        try:
            models.append(
                GoogleModelEntry(
                    model_id=normalize_model_id(str(raw_model.get("model_id", "") or raw_model.get("api_name", "") or "")),
                    api_name=str(raw_model.get("api_name", "") or f"models/{raw_model.get('model_id', '')}"),
                    display_name=str(raw_model.get("display_name", raw_model.get("model_id", "")) or raw_model.get("model_id", "")),
                    version=str(raw_model.get("version", "") or ""),
                    family=str(raw_model.get("family", _classify_family(str(raw_model.get("model_id", "") or ""))) or "other"),
                    is_alias=bool(raw_model.get("is_alias", False)),
                    input_token_limit=int(raw_model.get("input_token_limit", 0) or 0),
                    output_token_limit=int(raw_model.get("output_token_limit", 0) or 0),
                    supported_generation_methods=tuple(
                        str(method)
                        for method in (raw_model.get("supported_generation_methods") or [])
                    ),
                )
            )
        except Exception:
            continue
    return _sort_models_desc(models)


def _fetch_live_catalog() -> tuple[list[GoogleModelEntry], str]:
    if not settings.google_api_key:
        raise RuntimeError("No Google API key configured")
    request = Request(_MODELS_API_URL, headers={"x-goog-api-key": settings.google_api_key})
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    raw_models = payload.get("models") or []
    if not isinstance(raw_models, list):
        raise RuntimeError("Malformed Google models payload")
    models = _sort_models_desc(
        entry
        for raw_model in raw_models
        if (entry := _normalize_entry(raw_model)) is not None
    )
    if not models:
        raise RuntimeError("No eligible Gemini text models returned by Google")
    fetched_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return models, fetched_at_utc


def _cache_is_fresh(fetched_at_utc: str | None) -> bool:
    if not fetched_at_utc:
        return False
    try:
        fetched_at = datetime.fromisoformat(fetched_at_utc.replace("Z", "+00:00"))
    except ValueError:
        return False
    ttl = timedelta(hours=max(1, int(settings.model_catalog_cache_ttl_hours or 24)))
    return datetime.now(timezone.utc) - fetched_at <= ttl


def _select_auto_strong(models: list[GoogleModelEntry]) -> str:
    pro_models = [
        entry
        for entry in models
        if entry.family == "pro" and not entry.is_alias
    ]
    if pro_models:
        return _sort_models_desc(pro_models)[0].model_id
    alias_pro_models = [
        entry
        for entry in models
        if entry.family == "pro" and entry.is_alias
    ]
    if alias_pro_models:
        return _sort_models_desc(alias_pro_models)[0].model_id
    return _FALLBACK_STRONG_ALIAS


def _select_auto_weak(models: list[GoogleModelEntry]) -> str:
    concrete = [
        entry
        for entry in models
        if entry.family in {"flash-lite", "flash"} and not entry.is_alias
    ]
    if concrete:
        highest_generation = max(_parse_generation(entry.model_id) for entry in concrete)
        same_generation = [
            entry for entry in concrete
            if _parse_generation(entry.model_id) == highest_generation
        ]
        flash_lite = [entry for entry in same_generation if entry.family == "flash-lite"]
        if flash_lite:
            return _sort_models_desc(flash_lite)[0].model_id
        flash = [entry for entry in same_generation if entry.family == "flash"]
        if flash:
            return _sort_models_desc(flash)[0].model_id
        return _sort_models_desc(concrete)[0].model_id

    alias_flash_lite = [
        entry
        for entry in models
        if entry.family == "flash-lite" and entry.is_alias
    ]
    if alias_flash_lite:
        return _sort_models_desc(alias_flash_lite)[0].model_id
    alias_flash = [
        entry
        for entry in models
        if entry.family == "flash" and entry.is_alias
    ]
    if alias_flash:
        return _sort_models_desc(alias_flash)[0].model_id
    return _FALLBACK_WEAK_ALIAS


def _build_catalog(force_refresh: bool = False) -> tuple[list[GoogleModelEntry], str, str | None, str | None]:
    cache = _load_catalog_cache()
    if not force_refresh and cache and _cache_is_fresh(str(cache.get("fetched_at_utc", "") or "")):
        models = _deserialize_models(cache.get("models") or [])
        if models:
            return models, "cache", str(cache.get("fetched_at_utc", "") or None), None

    live_error: str | None = None
    if settings.google_api_key:
        try:
            live_models, fetched_at_utc = _fetch_live_catalog()
            auto_strong = _select_auto_strong(live_models)
            auto_weak = _select_auto_weak(live_models)
            _save_catalog_cache(
                live_models,
                fetched_at_utc=fetched_at_utc,
                auto_strong_model_id=auto_strong,
                auto_weak_model_id=auto_weak,
            )
            return live_models, "live", fetched_at_utc, None
        except (HTTPError, URLError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            live_error = str(exc)

    if cache:
        models = _deserialize_models(cache.get("models") or [])
        if models:
            return models, "cache", str(cache.get("fetched_at_utc", "") or None), live_error

    return _sort_models_desc(_built_in_catalog()), "fallback", None, live_error


def _legacy_override(current_value: str, default_value: str, explicit_override: str) -> str:
    normalized_override = normalize_model_id(explicit_override)
    if normalized_override:
        return normalized_override
    normalized_current = normalize_model_id(current_value)
    if normalized_current and normalized_current != default_value:
        return normalized_current
    return ""


def _explicit_env_keys() -> set[str]:
    keys = {
        key
        for key in os.environ
        if key.startswith("BOOMAI_")
    }
    env_paths = [Path(".env"), Path.home() / ".boomai" / ".env"]
    for env_path in env_paths:
        if not env_path.exists():
            continue
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key.startswith("BOOMAI_"):
                keys.add(key)
    return keys


def _read_env_value(key: str) -> str:
    value = os.environ.get(key)
    if value:
        return value.strip()
    env_paths = [Path(".env"), Path.home() / ".boomai" / ".env"]
    for env_path in env_paths:
        if not env_path.exists():
            continue
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            current_key, current_value = line.split("=", 1)
            if current_key.strip() == key:
                return current_value.strip()
    return ""


def _resolve_mode(mode_value: str, override_value: str) -> str:
    lowered = (mode_value or "auto").strip().lower()
    if lowered == "manual" and normalize_model_id(override_value):
        return "manual"
    return "auto"


def _label_for_model(model_id: str, catalog: Iterable[GoogleModelEntry]) -> str:
    normalized = normalize_model_id(model_id)
    for entry in catalog:
        if entry.model_id == normalized:
            return entry.display_name
    return normalized


def _build_strong_candidates(catalog: list[GoogleModelEntry]) -> list[GoogleModelEntry]:
    return _sort_models_desc(entry for entry in catalog if entry.family == "pro")


def _build_weak_candidates(catalog: list[GoogleModelEntry]) -> list[GoogleModelEntry]:
    return _sort_models_desc(entry for entry in catalog if entry.family in {"flash-lite", "flash"})


def _unique_chain(values: Iterable[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_model_id(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)


def _build_strong_fallback_chain(
    strong_model_id: str,
    catalog: list[GoogleModelEntry],
) -> tuple[str, ...]:
    pro_candidates = [entry.model_id for entry in _build_strong_candidates(catalog)]
    weak_candidates = [entry.model_id for entry in _build_weak_candidates(catalog)]
    return _unique_chain(
        [
            strong_model_id,
            *pro_candidates,
            *weak_candidates,
            _FALLBACK_STRONG_ALIAS,
            _FALLBACK_WEAK_ALIAS,
            _FALLBACK_WEAK_SECONDARY_ALIAS,
        ]
    )


def _build_weak_fallback_chain(
    weak_model_id: str,
    strong_model_id: str,
    catalog: list[GoogleModelEntry],
) -> tuple[str, ...]:
    weak_candidates = [entry.model_id for entry in _build_weak_candidates(catalog)]
    pro_candidates = [entry.model_id for entry in _build_strong_candidates(catalog)]
    return _unique_chain(
        [
            weak_model_id,
            *weak_candidates,
            _FALLBACK_WEAK_ALIAS,
            _FALLBACK_WEAK_SECONDARY_ALIAS,
            strong_model_id,
            *pro_candidates,
            _FALLBACK_STRONG_ALIAS,
        ]
    )


def get_runtime_models(*, force_refresh: bool = False) -> RuntimeModels:
    catalog, source, fetched_at_utc, catalog_error = _build_catalog(force_refresh=force_refresh)
    auto_strong = _select_auto_strong(catalog)
    auto_weak = _select_auto_weak(catalog)

    explicit_keys = _explicit_env_keys()
    strong_override = normalize_model_id(settings.strong_model_override)
    weak_override = normalize_model_id(settings.weak_model_override)
    strong_mode_value = settings.strong_model_mode
    weak_mode_value = settings.weak_model_mode

    if (
        not strong_override
        and "BOOMAI_STRONG_MODEL_MODE" not in explicit_keys
        and "BOOMAI_STRONG_MODEL_OVERRIDE" not in explicit_keys
    ):
        strong_override = _legacy_override(
            _read_env_value("BOOMAI_LLM_MODEL"),
            _LEGACY_DEFAULT_STRONG_MODEL,
            "",
        )
        if strong_override:
            strong_mode_value = "manual"

    if (
        not weak_override
        and "BOOMAI_WEAK_MODEL_MODE" not in explicit_keys
        and "BOOMAI_WEAK_MODEL_OVERRIDE" not in explicit_keys
    ):
        weak_override = _legacy_override(
            _read_env_value("BOOMAI_PATCH_LLM_MODEL"),
            _LEGACY_DEFAULT_WEAK_MODEL,
            "",
        )
        if weak_override:
            weak_mode_value = "manual"

    strong_mode = _resolve_mode(strong_mode_value, strong_override)
    weak_mode = _resolve_mode(weak_mode_value, weak_override)

    strong_model_id = strong_override if strong_mode == "manual" else auto_strong
    weak_model_id = weak_override if weak_mode == "manual" else auto_weak
    strong_fallback = _build_strong_fallback_chain(strong_model_id, catalog)
    weak_fallback = _build_weak_fallback_chain(weak_model_id, strong_model_id, catalog)

    return RuntimeModels(
        strong_model_id=strong_model_id,
        weak_model_id=weak_model_id,
        strong_display_name=_label_for_model(strong_model_id, catalog),
        weak_display_name=_label_for_model(weak_model_id, catalog),
        strong_mode=strong_mode,
        weak_mode=weak_mode,
        source=source,
        fetched_at_utc=fetched_at_utc,
        catalog=tuple(catalog),
        strong_candidates=tuple(_build_strong_candidates(catalog)),
        weak_candidates=tuple(_build_weak_candidates(catalog)),
        strong_fallback_models=strong_fallback,
        weak_fallback_models=weak_fallback,
        catalog_error=catalog_error,
    )


def apply_runtime_models(runtime_models: RuntimeModels) -> None:
    settings.strong_model = runtime_models.strong_model_id
    settings.weak_model = runtime_models.weak_model_id
