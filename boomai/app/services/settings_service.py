from __future__ import annotations

import sys
from pathlib import Path

from ...core.config import settings
from ...integrations.google.models_catalog_service import ModelCatalogService

GLOBAL_ENV_DIR = Path.home() / ".boomai"
GLOBAL_ENV_FILE = GLOBAL_ENV_DIR / ".env"
model_catalog_service = ModelCatalogService()


def save_setting(env_key: str, value: str) -> None:
    GLOBAL_ENV_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    if GLOBAL_ENV_FILE.exists():
        lines = GLOBAL_ENV_FILE.read_text(encoding="utf-8").splitlines()
    new_lines = [line for line in lines if not line.startswith(f"{env_key}=")]
    new_lines.append(f"{env_key}={value}")
    GLOBAL_ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def unset_setting(env_key: str) -> None:
    if not GLOBAL_ENV_FILE.exists():
        return
    lines = GLOBAL_ENV_FILE.read_text(encoding="utf-8").splitlines()
    new_lines = [line for line in lines if not line.startswith(f"{env_key}=")]
    if new_lines:
        GLOBAL_ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    else:
        GLOBAL_ENV_FILE.write_text("", encoding="utf-8")


def require_api_key() -> None:
    if settings.google_api_key:
        return
    print("\n  No API key found!")
    print("  Get one at: https://aistudio.google.com/apikey\n")
    key = input("  Enter your Gemini API key: ").strip()
    if not key:
        print("  Error: No key provided.")
        sys.exit(1)
    save_setting("BOOMAI_GOOGLE_API_KEY", key)
    settings.google_api_key = key
    print(f"  Key saved to {GLOBAL_ENV_FILE}")
    print()


def mask_api_key(key: str) -> str:
    if not key:
        return "not set"
    return key[:8] + "..." + key[-4:] if len(key) > 12 else "***"


def format_model_choice(entry) -> str:
    alias_suffix = " [alias]" if getattr(entry, "is_alias", False) else ""
    return f"{entry.display_name} [{entry.model_id}]{alias_suffix}"


def set_model_role(role: str, *, mode: str, override: str = "") -> None:
    mode_env = f"BOOMAI_{role.upper()}_MODEL_MODE"
    override_env = f"BOOMAI_{role.upper()}_MODEL_OVERRIDE"
    if mode == "auto":
        save_setting(mode_env, "auto")
        unset_setting(override_env)
        setattr(settings, f"{role}_model_mode", "auto")
        setattr(settings, f"{role}_model_override", "")
        return

    normalized_override = override.strip()
    if not normalized_override:
        return
    save_setting(mode_env, "manual")
    save_setting(override_env, normalized_override)
    setattr(settings, f"{role}_model_mode", "manual")
    setattr(settings, f"{role}_model_override", normalized_override)


def refresh_runtime_model_catalog(*, force_refresh: bool = False):
    runtime_models = model_catalog_service.get_runtime_models(force_refresh=force_refresh)
    model_catalog_service.apply_runtime_models(runtime_models)
    return runtime_models
