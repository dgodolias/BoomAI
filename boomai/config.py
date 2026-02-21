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

    # GitHub
    github_token: str = ""
    github_repository: str = ""
    pr_number: int = 0

    # Slack
    slack_webhook_url: str = ""
    slack_enabled: bool = True

    # Review settings
    max_findings: int = 20
    max_diff_chars: int = 250_000

    # Scan settings (full-codebase mode)
    max_scan_chars: int = 1_500_000
    scan_max_files: int = 1000
    scan_output_tokens: int = 65536
    scan_timeout: float = 600.0

    model_config = {
        "env_file": (".env", str(_global_env)),
        "env_file_encoding": "utf-8",
        "env_prefix": "BOOMAI_",
    }


settings = Settings()
