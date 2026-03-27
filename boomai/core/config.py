from pathlib import Path

from pydantic_settings import BaseSettings

# Search for .env in: CWD first, then ~/.boomai/.env (global)
_global_env = Path.home() / ".boomai" / ".env"


class Settings(BaseSettings):
    # Gemini API (same pattern as DataViz)
    google_api_key: str = ""
    llm_model: str = "gemini-3.1-pro-preview-customtools"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/models"
    max_output_tokens: int = 32768
    llm_timeout: float = 300.0

    # Scan settings (full-codebase mode)
    max_scan_chars: int = 200_000
    scan_max_files: int = 1500
    scan_output_tokens: int = 65536
    scan_timeout: float = 180.0
    scan_debug: bool = False
    scan_comments: bool = False
    scan_explanations: bool = True

    # Scan planning (repo-map phase)
    plan_output_tokens: int = 65536
    plan_timeout: float = 180.0

    model_config = {
        "env_file": (".env", str(_global_env)),
        "env_file_encoding": "utf-8",
        "env_prefix": "BOOMAI_",
        "extra": "ignore",
    }


settings = Settings()
