from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Gemini API (same pattern as DataViz)
    google_api_key: str = ""
    llm_model: str = "gemini-3.1-pro-preview-customtools"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/models"
    max_output_tokens: int = 16384
    llm_timeout: float = 120.0

    # GitHub
    github_token: str = ""
    github_repository: str = ""
    pr_number: int = 0

    # Slack
    slack_webhook_url: str = ""
    slack_enabled: bool = True

    # Review settings
    max_findings: int = 20
    max_diff_chars: int = 100_000

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "env_prefix": "BOOMAI_",
    }


settings = Settings()
