from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingSource(str, Enum):
    SEMGREP = "semgrep"
    DEVSKIM = "devskim"
    ROSLYN = "roslyn"
    GITLEAKS = "gitleaks"
    AI = "ai"


class Finding(BaseModel):
    file: str
    line: int
    end_line: int | None = None
    severity: Severity
    source: FindingSource
    rule_id: str
    message: str
    suggestion: str | None = None


class IssueSeed(BaseModel):
    """Normalized non-AI issue seed used to guide retrieval and review."""
    file: str
    line: int
    end_line: int | None = None
    severity: Severity
    source: FindingSource
    rule_id: str
    message: str


class ReviewComment(BaseModel):
    file: str
    line: int
    end_line: int | None = None
    severity: Severity = Severity.MEDIUM
    body: str
    suggestion: str | None = None
    old_code: str | None = None


@dataclass
class UsageStats:
    """Accumulated token usage from Gemini API responses."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    api_calls: int = 0
    per_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, result: dict, model_name: str | None = None) -> None:
        meta = result.get("usageMetadata", {})
        prompt = meta.get("promptTokenCount", 0)
        completion = meta.get("candidatesTokenCount", 0)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.api_calls += 1
        if model_name:
            bucket = self.per_model.setdefault(
                model_name, {"prompt_tokens": 0, "completion_tokens": 0, "api_calls": 0}
            )
            bucket["prompt_tokens"] += prompt
            bucket["completion_tokens"] += completion
            bucket["api_calls"] += 1


class ReviewSummary(BaseModel):
    summary: str
    findings: list[ReviewComment]
    critical_count: int = 0
    has_critical: bool = False
    usage: UsageStats | None = None

    model_config = {"arbitrary_types_allowed": True}
