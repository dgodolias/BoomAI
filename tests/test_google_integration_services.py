from __future__ import annotations

from boomai.integrations.google.models_catalog_service import ModelCatalogService
from boomai.integrations.google.pricing_catalog_service import PricingCatalogService


def test_model_catalog_service_delegates(monkeypatch) -> None:
    service = ModelCatalogService()
    sentinel = object()
    called: list[object] = []

    import boomai.integrations.google.models_catalog_service as module

    monkeypatch.setattr(module, "get_runtime_models", lambda force_refresh=False: sentinel)
    monkeypatch.setattr(module, "apply_runtime_models", lambda runtime_models: called.append(runtime_models))

    resolved = service.get_runtime_models(force_refresh=True)
    service.apply_runtime_models(sentinel)

    assert resolved is sentinel
    assert called == [sentinel]


def test_pricing_catalog_service_delegates(monkeypatch) -> None:
    service = PricingCatalogService()
    sentinel_metadata = {"source": "cache"}

    import boomai.integrations.google.pricing_catalog_service as module

    monkeypatch.setattr(module, "get_pricing", lambda model_id: (f"pricing:{model_id}", True))
    monkeypatch.setattr(module, "get_pricing_catalog", lambda force_refresh=False: "catalog")
    monkeypatch.setattr(module, "get_pricing_catalog_metadata", lambda: sentinel_metadata)

    pricing, known = service.get_pricing("gemini-x")
    catalog = service.get_catalog(force_refresh=True)
    metadata = service.get_metadata()

    assert pricing == "pricing:gemini-x"
    assert known is True
    assert catalog == "catalog"
    assert metadata is sentinel_metadata
