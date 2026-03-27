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

    def add(self, result: dict) -> None:
        meta = result.get("usageMetadata", {})
        self.prompt_tokens += meta.get("promptTokenCount", 0)
        self.completion_tokens += meta.get("candidatesTokenCount", 0)
        self.api_calls += 1


class ReviewSummary(BaseModel):
    summary: str
    findings: list[ReviewComment]
    critical_count: int = 0
    has_critical: bool = False
    usage: UsageStats | None = None

    model_config = {"arbitrary_types_allowed": True}
