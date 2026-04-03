from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .config import settings
from .google_models import normalize_model_id

_PRICING_SCHEMA_VERSION = 1
_PRICING_CACHE_PATH = Path.home() / ".boomai" / "pricing_catalog.json"
_PRICING_PAGE_URL = "https://ai.google.dev/gemini-api/docs/pricing"
_HIGH_CONTEXT_THRESHOLD = 200_000
_MEMOIZED_CATALOG: ResolvedPricingCatalog | None = None

_SECTION_RE = re.compile(r"<h2[^>]*>(?P<title>.*?)</h2>(?P<body>.*?)(?=<h2\b|$)", re.I | re.S)
_SUBSECTION_RE = re.compile(
    r"<h3[^>]*>(?P<name>.*?)</h3>\s*<table[^>]*class=\"pricing-table\"[^>]*>(?P<table>.*?)</table>",
    re.I | re.S,
)
_ROW_RE = re.compile(r"<tr[^>]*>(?P<body>.*?)</tr>", re.I | re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.I | re.S)
_CODE_MODEL_RE = re.compile(r"<code[^>]*>(gemini-[^<]+)</code>", re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_USD_RE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)")


class ModelPricing(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_per_m: float
    output_per_m: float
    label: str
    cached_input_per_m: float | None = None
    input_per_m_high: float | None = None
    output_per_m_high: float | None = None
    cached_input_per_m_high: float | None = None


class PricingSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    section_name: str
    paid_tier_rows: dict[str, str] = Field(default_factory=dict)
    pricing: ModelPricing | None = None


class PricingModelEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: str
    label: str
    sections: dict[str, PricingSection] = Field(default_factory=dict)
    standard_pricing: ModelPricing | None = None


class PricingCatalog(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int
    fetched_at_utc: str | None = None
    source_url: str
    models: list[PricingModelEntry] = Field(default_factory=list)


class ResolvedPricingCatalog(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: Literal["live", "cache", "fallback"]
    fetched_at_utc: str | None = None
    source_url: str = _PRICING_PAGE_URL
    models: list[PricingModelEntry] = Field(default_factory=list)
    error: str | None = None


def _clean_html_text(value: str) -> str:
    text = _BR_RE.sub("\n", value)
    text = re.sub(r"</(?:p|div|section|li|ul|ol|tbody|thead|table|tr|h\d)>", "\n", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _extract_usd_amount(line: str) -> float | None:
    match = _USD_RE.search(line)
    if not match:
        return None
    return float(match.group(1))


def _extract_low_high_from_html(cell_html: str) -> tuple[float | None, float | None]:
    raw_lines = re.split(r"<br\s*/?>", cell_html, flags=re.I)
    lines = []
    for raw_line in raw_lines:
        clean_line = _clean_html_text(raw_line)
        if not clean_line:
            continue
        if "storage price" in clean_line.lower():
            continue
        if _USD_RE.search(raw_line) or _USD_RE.search(clean_line):
            lines.append(clean_line)
    if not lines:
        return None, None

    threshold_lines = [
        line for line in lines
        if "prompt" in line.lower() and ("<=" in line or ">" in line)
    ]
    if threshold_lines:
        low = _extract_usd_amount(threshold_lines[0])
        high = _extract_usd_amount(threshold_lines[1]) if len(threshold_lines) > 1 else low
        return low, high if high is not None else low

    first = _extract_usd_amount(lines[0])
    return first, first


def _parse_standard_pricing(label: str, raw_rows: dict[str, str]) -> ModelPricing | None:
    def row_html(prefix: str) -> str:
        for row_name, raw_html in raw_rows.items():
            if row_name.lower().startswith(prefix.lower()):
                return raw_html
        return ""

    input_low, input_high = _extract_low_high_from_html(row_html("Input price"))
    output_low, output_high = _extract_low_high_from_html(row_html("Output price"))
    cached_low, cached_high = _extract_low_high_from_html(row_html("Context caching price"))
    if input_low is None or output_low is None:
        return None
    return ModelPricing(
        input_per_m=input_low,
        output_per_m=output_low,
        label=label,
        cached_input_per_m=cached_low,
        input_per_m_high=input_high if input_high is not None else input_low,
        output_per_m_high=output_high if output_high is not None else output_low,
        cached_input_per_m_high=cached_high if cached_high is not None else cached_low,
    )


def _parse_section_table(section_name: str, table_html: str, label: str) -> PricingSection:
    rows: dict[str, str] = {}
    raw_rows: dict[str, str] = {}
    for row_match in _ROW_RE.finditer(table_html):
        cells = _CELL_RE.findall(row_match.group("body"))
        if len(cells) < 3:
            continue
        row_name = _clean_html_text(cells[0])
        paid_tier = _clean_html_text(cells[2])
        if row_name:
            rows[row_name] = paid_tier
            raw_rows[row_name] = cells[2]
    pricing = _parse_standard_pricing(label, raw_rows) if section_name.lower() == "standard" else None
    return PricingSection(section_name=section_name, paid_tier_rows=rows, pricing=pricing)


def _parse_pricing_html(html: str) -> list[PricingModelEntry]:
    entries: dict[str, PricingModelEntry] = {}
    for section_match in _SECTION_RE.finditer(html):
        title = _clean_html_text(section_match.group("title"))
        body = section_match.group("body")
        model_ids = [
            normalize_model_id(model_id)
            for model_id in _CODE_MODEL_RE.findall(body[:5000])
        ]
        if not model_ids:
            continue
        sections: dict[str, PricingSection] = {}
        for subsection_match in _SUBSECTION_RE.finditer(body):
            subsection_name = _clean_html_text(subsection_match.group("name"))
            if not subsection_name:
                continue
            section = _parse_section_table(subsection_name, subsection_match.group("table"), title)
            sections[subsection_name.lower()] = section
        standard_pricing = sections.get("standard").pricing if "standard" in sections else None
        for model_id in model_ids:
            if not model_id.startswith("gemini-"):
                continue
            entries[model_id] = PricingModelEntry(
                model_id=model_id,
                label=title,
                sections=sections,
                standard_pricing=standard_pricing,
            )
    return sorted(entries.values(), key=lambda entry: entry.model_id)


def _built_in_catalog() -> PricingCatalog:
    def entry(model_id: str, pricing: ModelPricing) -> PricingModelEntry:
        return PricingModelEntry(
            model_id=model_id,
            label=pricing.label,
            sections={"standard": PricingSection(section_name="Standard", pricing=pricing)},
            standard_pricing=pricing,
        )

    models = [
        entry(
            "gemini-3.1-pro-preview",
            ModelPricing(
                input_per_m=2.00,
                output_per_m=12.00,
                label="Gemini 3.1 Pro Preview",
                cached_input_per_m=0.20,
                input_per_m_high=4.00,
                output_per_m_high=18.00,
                cached_input_per_m_high=0.40,
            ),
        ),
        entry(
            "gemini-3.1-flash-lite-preview",
            ModelPricing(
                input_per_m=0.25,
                output_per_m=1.50,
                label="Gemini 3.1 Flash-Lite Preview",
                cached_input_per_m=0.025,
                input_per_m_high=0.25,
                output_per_m_high=1.50,
                cached_input_per_m_high=0.025,
            ),
        ),
        entry("gemini-3-pro-preview", ModelPricing(input_per_m=1.25, output_per_m=10.00, label="Gemini 3 Pro Preview")),
        entry("gemini-3-flash-preview", ModelPricing(input_per_m=0.50, output_per_m=3.00, label="Gemini 3 Flash Preview")),
        entry(
            "gemini-2.5-pro",
            ModelPricing(
                input_per_m=1.25,
                output_per_m=10.00,
                label="Gemini 2.5 Pro",
                cached_input_per_m=0.125,
                input_per_m_high=2.50,
                output_per_m_high=15.00,
                cached_input_per_m_high=0.25,
            ),
        ),
        entry(
            "gemini-2.5-flash",
            ModelPricing(
                input_per_m=0.30,
                output_per_m=2.50,
                label="Gemini 2.5 Flash",
                cached_input_per_m=0.03,
                input_per_m_high=0.30,
                output_per_m_high=2.50,
                cached_input_per_m_high=0.03,
            ),
        ),
        entry(
            "gemini-2.5-flash-lite-preview-09-2025",
            ModelPricing(
                input_per_m=0.10,
                output_per_m=0.40,
                label="Gemini 2.5 Flash-Lite Preview",
                cached_input_per_m=0.01,
                input_per_m_high=0.10,
                output_per_m_high=0.40,
                cached_input_per_m_high=0.01,
            ),
        ),
        entry(
            "gemini-2.5-flash-lite",
            ModelPricing(
                input_per_m=0.10,
                output_per_m=0.40,
                label="Gemini 2.5 Flash-Lite",
                cached_input_per_m=0.01,
                input_per_m_high=0.10,
                output_per_m_high=0.40,
                cached_input_per_m_high=0.01,
            ),
        ),
    ]
    return PricingCatalog(
        schema_version=_PRICING_SCHEMA_VERSION,
        fetched_at_utc=None,
        source_url=_PRICING_PAGE_URL,
        models=models,
    )


def _load_cache() -> PricingCatalog | None:
    if not _PRICING_CACHE_PATH.exists():
        return None
    try:
        payload = json.loads(_PRICING_CACHE_PATH.read_text(encoding="utf-8"))
        catalog = PricingCatalog.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if catalog.schema_version != _PRICING_SCHEMA_VERSION:
        return None
    return catalog


def _save_cache(catalog: PricingCatalog) -> None:
    _PRICING_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PRICING_CACHE_PATH.write_text(
        json.dumps(catalog.model_dump(mode="json"), indent=2, ensure_ascii=True),
        encoding="utf-8",
    )


def _cache_is_fresh(fetched_at_utc: str | None) -> bool:
    if not fetched_at_utc:
        return False
    try:
        fetched_at = datetime.fromisoformat(fetched_at_utc.replace("Z", "+00:00"))
    except ValueError:
        return False
    ttl = timedelta(hours=max(1, int(settings.pricing_catalog_cache_ttl_hours or 24)))
    return datetime.now(timezone.utc) - fetched_at <= ttl


def _fetch_live_catalog() -> PricingCatalog:
    response = httpx.get(_PRICING_PAGE_URL, timeout=30, follow_redirects=True)
    response.raise_for_status()
    entries = _parse_pricing_html(response.text)
    if not entries:
        raise RuntimeError("No pricing entries parsed from Google pricing page")
    return PricingCatalog(
        schema_version=_PRICING_SCHEMA_VERSION,
        fetched_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        source_url=_PRICING_PAGE_URL,
        models=entries,
    )


def get_pricing_catalog(*, force_refresh: bool = False) -> ResolvedPricingCatalog:
    global _MEMOIZED_CATALOG
    if not force_refresh and _MEMOIZED_CATALOG is not None:
        if _MEMOIZED_CATALOG.source != "cache" or _cache_is_fresh(_MEMOIZED_CATALOG.fetched_at_utc):
            return _MEMOIZED_CATALOG

    cache = _load_cache()
    if not force_refresh and cache and _cache_is_fresh(cache.fetched_at_utc):
        _MEMOIZED_CATALOG = ResolvedPricingCatalog(
            source="cache",
            fetched_at_utc=cache.fetched_at_utc,
            source_url=cache.source_url,
            models=cache.models,
        )
        return _MEMOIZED_CATALOG

    live_error: str | None = None
    try:
        live_catalog = _fetch_live_catalog()
        _save_cache(live_catalog)
        _MEMOIZED_CATALOG = ResolvedPricingCatalog(
            source="live",
            fetched_at_utc=live_catalog.fetched_at_utc,
            source_url=live_catalog.source_url,
            models=live_catalog.models,
        )
        return _MEMOIZED_CATALOG
    except (httpx.HTTPError, RuntimeError, OSError, ValueError) as exc:
        live_error = str(exc)

    if cache:
        _MEMOIZED_CATALOG = ResolvedPricingCatalog(
            source="cache",
            fetched_at_utc=cache.fetched_at_utc,
            source_url=cache.source_url,
            models=cache.models,
            error=live_error,
        )
        return _MEMOIZED_CATALOG

    fallback = _built_in_catalog()
    _MEMOIZED_CATALOG = ResolvedPricingCatalog(
        source="fallback",
        fetched_at_utc=fallback.fetched_at_utc,
        source_url=fallback.source_url,
        models=fallback.models,
        error=live_error,
    )
    return _MEMOIZED_CATALOG


def get_pricing(model_id: str) -> tuple[ModelPricing, bool]:
    normalized = normalize_model_id(model_id)
    catalog = get_pricing_catalog()
    exact_match = next((entry for entry in catalog.models if entry.model_id == normalized), None)
    if exact_match and exact_match.standard_pricing is not None:
        return exact_match.standard_pricing, True

    prefix_matches = [
        entry
        for entry in catalog.models
        if normalized.startswith(entry.model_id) and entry.standard_pricing is not None
    ]
    if prefix_matches:
        prefix_matches.sort(key=lambda entry: len(entry.model_id), reverse=True)
        return prefix_matches[0].standard_pricing, True

    unknown = ModelPricing(
        input_per_m=1.25,
        output_per_m=10.00,
        label=f"Unknown model ({normalized or 'n/a'})",
    )
    return unknown, False


def get_pricing_catalog_metadata() -> dict[str, str | None]:
    catalog = get_pricing_catalog()
    return {
        "source": catalog.source,
        "fetched_at_utc": catalog.fetched_at_utc,
        "source_url": catalog.source_url,
        "error": catalog.error,
    }


def effective_rates(pricing: ModelPricing, prompt_tokens: int) -> tuple[float, float, float]:
    use_high = prompt_tokens > _HIGH_CONTEXT_THRESHOLD
    input_rate = pricing.input_per_m_high if use_high and pricing.input_per_m_high is not None else pricing.input_per_m
    output_rate = pricing.output_per_m_high if use_high and pricing.output_per_m_high is not None else pricing.output_per_m
    cached_rate = (
        pricing.cached_input_per_m_high
        if use_high and pricing.cached_input_per_m_high is not None
        else pricing.cached_input_per_m
    )
    if cached_rate is None:
        cached_rate = input_rate
    return input_rate, output_rate, cached_rate
