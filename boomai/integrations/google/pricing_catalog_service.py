from __future__ import annotations

from ...core.google_pricing import (
    ModelPricing,
    get_pricing,
    get_pricing_catalog,
    get_pricing_catalog_metadata,
)


class PricingCatalogService:
    """Application-facing service for pricing resolution and catalog metadata."""

    def get_pricing(self, model_id: str) -> tuple[ModelPricing, bool]:
        return get_pricing(model_id)

    def get_catalog(self, *, force_refresh: bool = False):
        return get_pricing_catalog(force_refresh=force_refresh)

    def get_metadata(self) -> dict[str, str | None]:
        return get_pricing_catalog_metadata()
