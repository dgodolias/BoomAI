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
    category: str | None = None
    confidence: str | None = None
    fixable: bool | None = None
    patch_group_key: str | None = None
    suggestion: str | None = None
    old_code: str | None = None


@dataclass
class UsageStats:
    """Accumulated token usage from Gemini API responses."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    api_calls: int = 0
    per_model: dict[str, dict[str, int]] = field(default_factory=dict)
    per_stage: dict[str, dict[str, int]] = field(default_factory=dict)
    per_stage_model: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    usage_metadata_totals: dict[str, int] = field(default_factory=dict)
    request_events: list[dict[str, object]] = field(default_factory=list)

    @staticmethod
    def _numeric_usage_fields(meta: dict) -> dict[str, int]:
        numeric: dict[str, int] = {}
        for key, value in meta.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                numeric[key] = int(value)
        return numeric

    @staticmethod
    def _merge_bucket(bucket: dict[str, int], values: dict[str, int]) -> None:
        for key, value in values.items():
            bucket[key] = bucket.get(key, 0) + value

    def add(
        self,
        result: dict,
        model_name: str | None = None,
        *,
        stage: str | None = None,
        request_label: str | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        meta = result.get("usageMetadata", {})
        prompt = int(meta.get("promptTokenCount", 0) or 0)
        completion = int(meta.get("candidatesTokenCount", 0) or 0)
        numeric_meta = self._numeric_usage_fields(meta)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.api_calls += 1
        self._merge_bucket(self.usage_metadata_totals, numeric_meta)

        if model_name:
            bucket = self.per_model.setdefault(
                model_name, {"prompt_tokens": 0, "completion_tokens": 0, "api_calls": 0}
            )
            self._merge_bucket(bucket, numeric_meta)
            bucket["prompt_tokens"] += prompt
            bucket["completion_tokens"] += completion
            bucket["api_calls"] += 1

        if stage:
            stage_bucket = self.per_stage.setdefault(
                stage, {"prompt_tokens": 0, "completion_tokens": 0, "api_calls": 0}
            )
            self._merge_bucket(stage_bucket, numeric_meta)
            stage_bucket["prompt_tokens"] += prompt
            stage_bucket["completion_tokens"] += completion
            stage_bucket["api_calls"] += 1

            if model_name:
                stage_models = self.per_stage_model.setdefault(stage, {})
                stage_model_bucket = stage_models.setdefault(
                    model_name, {"prompt_tokens": 0, "completion_tokens": 0, "api_calls": 0}
                )
                self._merge_bucket(stage_model_bucket, numeric_meta)
                stage_model_bucket["prompt_tokens"] += prompt
                stage_model_bucket["completion_tokens"] += completion
                stage_model_bucket["api_calls"] += 1

        event: dict[str, object] = {
            "model": model_name or "",
            "stage": stage or "unknown",
            "request_label": request_label or "",
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "usage_metadata": numeric_meta,
        }
        if extra:
            event["extra"] = extra
        self.request_events.append(event)

    def annotate_last_event(
        self,
        *,
        stage: str | None = None,
        request_label: str | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        """Merge additional metadata into the most recent matching request event."""
        if not extra:
            return
        for event in reversed(self.request_events):
            if stage is not None and event.get("stage") != stage:
                continue
            if request_label is not None and event.get("request_label") != request_label:
                continue
            existing = event.get("extra")
            if not isinstance(existing, dict):
                existing = {}
                event["extra"] = existing
            existing.update(extra)
            return


class ReviewSummary(BaseModel):
    summary: str
    findings: list[ReviewComment]
    critical_count: int = 0
    has_critical: bool = False
    usage: UsageStats | None = None

    model_config = {"arbitrary_types_allowed": True}
