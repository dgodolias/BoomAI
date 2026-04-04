from __future__ import annotations

from ...core.google_models import (
    RuntimeModels,
    apply_runtime_models,
    get_runtime_models,
)


class ModelCatalogService:
    """Application-facing service for runtime Gemini model resolution."""

    def get_runtime_models(self, *, force_refresh: bool = False) -> RuntimeModels:
        return get_runtime_models(force_refresh=force_refresh)

    def apply_runtime_models(self, runtime_models: RuntimeModels) -> None:
        apply_runtime_models(runtime_models)
