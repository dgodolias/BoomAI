from pydantic import BaseModel
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingSource(str, Enum):
    SEMGREP = "semgrep"
    ROSLYN = "roslyn"
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


class ReviewComment(BaseModel):
    file: str
    line: int
    end_line: int | None = None
    body: str
    suggestion: str | None = None
    old_code: str | None = None


class ReviewSummary(BaseModel):
    summary: str
    findings: list[ReviewComment]
    critical_count: int = 0
    has_critical: bool = False
