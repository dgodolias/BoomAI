"""Gemini AI review integration — same httpx pattern as DataViz."""

import asyncio
import json
import logging
import re
import time
from typing import Callable

import httpx

from ..core.config import settings
from ..core.models import IssueSeed, ReviewComment, ReviewSummary, Severity, UsageStats
from .pack_selector import select_prompt_pack_ids
from .prompts import (
    build_scan_system_prompt, build_scan_user_message,
    build_plan_prompt, build_plan_response_schema, build_plan_user_message,
    build_scan_response_schema,
    build_fix_response_schema, build_fix_system_prompt, build_fix_user_message,
)

logger = logging.getLogger(__name__)

# Type alias for progress callback
ProgressFn = Callable[[str], None] | None

# Model fallback chain — each model has separate rate limits (RPM/RPD)
_FALLBACK_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite-preview-09-2025",
]

_PATCH_FALLBACK_MODELS = [
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite-preview-09-2025",
    "gemini-3.1-pro-preview",
    "gemini-2.5-pro",
]


class _ModelChain:
    """Thread-safe model fallback chain for 429 rate limits."""

    def __init__(self, initial_model: str, models: list[str] | None = None):
        self._models = list(models or _FALLBACK_MODELS)
        try:
            self._index = self._models.index(initial_model)
        except ValueError:
            self._models.insert(0, initial_model)
            self._index = 0
        self._lock = asyncio.Lock()

    @property
    def current(self) -> str:
        return self._models[self._index]

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self._models)

    async def advance(self, failed_model: str) -> str | None:
        """Advance past failed_model. Returns new model or None if exhausted."""
        async with self._lock:
            if self.current != failed_model:
                return self.current if not self.exhausted else None
            self._index += 1
            return self.current if not self.exhausted else None

    def build_url(self) -> str:
        return (
            f"{settings.gemini_base_url}/{self.current}"
            f":generateContent?key={settings.google_api_key}"
        )


async def _with_heartbeat(
    coro, emit: ProgressFn, label: str = "Waiting for Gemini", interval: int = 10,
):
    """Run *coro* while printing elapsed time every *interval* seconds."""
    if emit is None:
        return await coro
    start = time.monotonic()

    async def _beat():
        while True:
            await asyncio.sleep(interval)
            elapsed = int(time.monotonic() - start)
            emit(f"  {label}... ({elapsed}s)")

    task = asyncio.create_task(_beat())
    try:
        return await coro
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _gemini_post(
    url: str, payload: dict, timeout: float,
    max_retries: int = 3,
    on_retry: Callable[[int, str], None] | None = None,
) -> httpx.Response | None:
    """POST to Gemini API with retry on transient network errors."""
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                )
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.ReadError,
            httpx.ProtocolError,
            httpx.RemoteProtocolError,
        ) as e:
            error_type = type(e).__name__
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                if on_retry:
                    on_retry(attempt + 1, f"{error_type}, retrying in {wait}s")
                else:
                    logger.warning(f"Gemini API attempt {attempt + 1} failed ({error_type}), retrying in {wait}s...")
                await asyncio.sleep(wait)
            else:
                if on_retry:
                    on_retry(attempt + 1, f"{error_type}, giving up after {max_retries} attempts")
                else:
                    logger.error(f"Gemini API failed after {max_retries} attempts: {error_type}")
                return None
    return None


def _sanitize_json(text: str) -> str:
    """Fix common Gemini JSON issues: trailing commas."""
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


def _compute_effective_timeout(chunk_chars: int, file_count: int) -> float:
    """Scale chunk timeout with payload size instead of file count alone."""
    timeout = settings.scan_timeout
    if file_count == 1:
        timeout *= 2
    if chunk_chars >= 100_000:
        timeout = max(timeout, settings.scan_timeout + 180)
    elif chunk_chars >= 60_000:
        timeout = max(timeout, settings.scan_timeout + 90)
    return timeout


def _compute_scan_output_tokens(
    chunk_chars: int,
    file_count: int,
    issue_seed_count: int = 0,
) -> int:
    """Choose a tighter output cap based on chunk complexity.

    Keeps the configured setting as a hard upper bound while avoiding
    a huge maxOutputTokens budget for small scans.
    """
    cap = 4096
    if chunk_chars >= 50_000 or file_count >= 6:
        cap = 8192
    if chunk_chars >= 100_000 or file_count >= 10 or issue_seed_count >= 8:
        cap = 16384
    if chunk_chars >= 200_000 or file_count >= 20 or issue_seed_count >= 16:
        cap = 24576
    if chunk_chars >= 320_000 or file_count >= 35 or issue_seed_count >= 24:
        cap = 32768
    return min(settings.scan_output_tokens, cap)


def _effective_scan_char_budget(char_budget: int) -> int:
    """Reserve room for prompt wrappers, issue seeds, and related snippets."""
    return max(40_000, char_budget - settings.scan_chunk_reserved_chars)


def _compute_scan_concurrency(model_name: str, chunk_count: int) -> int:
    """Choose safer concurrency for pro models and higher throughput for flash."""
    lowered = model_name.lower()
    if "flash" in lowered:
        limit = settings.scan_flash_max_concurrency
        if chunk_count >= 12:
            limit = min(limit, 3)
        return max(1, limit)

    limit = settings.scan_pro_max_concurrency
    if chunk_count <= 3:
        limit = min(limit + 1, settings.scan_flash_max_concurrency)
    return max(1, limit)


def _compute_patch_concurrency(model_name: str, patch_count: int) -> int:
    """Use more parallelism for flash patch generation, less for pro."""
    lowered = model_name.lower()
    limit = settings.patch_max_concurrency
    if "flash" not in lowered:
        limit = min(limit, 2)
    if patch_count <= 2:
        limit = min(limit, patch_count)
    return max(1, limit)


def _is_gemini3_family(model_name: str) -> bool:
    lowered = model_name.lower()
    return "gemini-3" in lowered or "gemini-3.1" in lowered


def _is_flash_model(model_name: str) -> bool:
    return "flash" in model_name.lower()


def _is_pro_model(model_name: str) -> bool:
    lowered = model_name.lower()
    return "pro" in lowered and "flash" not in lowered


def _normalize_scan_output_tokens(model_name: str, requested_output_tokens: int) -> int:
    """Leave enough output headroom for Gemini 3 Pro structured JSON."""
    effective = max(1, int(requested_output_tokens))
    if _is_gemini3_family(model_name) and _is_pro_model(model_name):
        effective = max(effective, settings.scan_pro_min_output_tokens)
    return min(settings.scan_output_tokens, effective)


def _build_generation_config(
    model_name: str,
    max_output_tokens: int,
    response_json_schema: dict | None = None,
) -> dict:
    """Build generation config with model-specific stability controls."""
    config = {
        "maxOutputTokens": max_output_tokens,
        "temperature": 0.1,
        "responseMimeType": "application/json",
    }
    if response_json_schema is not None:
        config["responseJsonSchema"] = response_json_schema

    lowered = model_name.lower()
    if _is_gemini3_family(model_name):
        if _is_flash_model(model_name):
            config["thinkingConfig"] = {"thinkingLevel": settings.gemini3_flash_thinking_level}
        elif _is_pro_model(model_name):
            config["thinkingConfig"] = {"thinkingLevel": settings.gemini3_pro_thinking_level}
    elif "flash" in lowered:
        config["thinkingConfig"] = {"thinkingBudget": 0}

    return config


def _recover_truncated_json(text: str) -> dict | None:
    """
    Recover a valid response dict from a Gemini response truncated mid-string.

    Strategy: extract the summary (if complete) then use raw_decode to pull
    out each individual finding object one at a time, stopping at the first
    truncated/invalid object.  Returns a dict or None if nothing recoverable.
    """
    import re

    # Extract summary — only if the string is fully closed
    summary = "Review completed (output truncated)."
    summary_match = re.search(r'"summary"\s*:\s*("(?:[^"\\]|\\.)*")', text)
    if summary_match:
        try:
            summary = json.loads(summary_match.group(1))
        except json.JSONDecodeError:
            pass

    # Find the start of the findings array
    findings_array_match = re.search(r'"findings"\s*:\s*\[', text)
    if not findings_array_match:
        return None

    decoder = json.JSONDecoder()
    findings: list[dict] = []
    pos = findings_array_match.end()

    while pos < len(text):
        # Skip whitespace and commas between objects
        while pos < len(text) and text[pos] in ' \t\n\r,':
            pos += 1
        if pos >= len(text) or text[pos] in (']', '}'):
            break
        if text[pos] != '{':
            break
        try:
            obj, end_pos = decoder.raw_decode(text, pos)
            findings.append(obj)
            pos = end_pos
        except json.JSONDecodeError:
            break  # rest of array is truncated — stop here

    if not findings and summary == "Review completed (output truncated).":
        return None  # nothing useful recovered

    return {"summary": summary, "findings": findings, "critical_count": 0}


def _parse_review_response(text: str) -> tuple[ReviewSummary, str]:
    """Parse Gemini's JSON response into a ReviewSummary.

    Returns (summary, status) where status is one of:
    - "full": parsed full JSON successfully
    - "recovered": salvaged partial JSON after truncation
    - "failed": could not recover a usable payload
    """
    def _build_summary(data: dict) -> ReviewSummary:
        comments = []
        for f in data.get("findings", []):
            raw_severity = str(f.get("severity", Severity.MEDIUM.value)).lower()
            try:
                severity = Severity(raw_severity)
            except ValueError:
                severity = Severity.MEDIUM
            comments.append(
                ReviewComment(
                    file=f["file"],
                    line=f["line"],
                    end_line=f.get("end_line"),
                    severity=severity,
                    body=f["message"],
                    category=str(f.get("category", "") or "").lower() or None,
                    confidence=str(f.get("confidence", "") or "").lower() or None,
                    fixable=f.get("fixable") if isinstance(f.get("fixable"), bool) else None,
                    patch_group_key=str(f.get("patch_group_key", "") or "") or None,
                    suggestion=f.get("suggestion"),
                    old_code=f.get("old_code"),
                )
            )
        critical_count = data.get("critical_count", 0)
        return ReviewSummary(
            summary=data.get("summary", "Review completed."),
            findings=comments,
            critical_count=critical_count,
            has_critical=critical_count > 0,
        )

    # --- primary parse ---
    try:
        sanitized = _sanitize_json(text)
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(sanitized)
        return _build_summary(data), "full"
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        if settings.scan_debug:
            logger.warning(f"Failed to parse Gemini response: {e}\nRaw: {text[:500]}")
        else:
            logger.debug("Failed to parse Gemini response; attempting truncation recovery")

    # --- truncation recovery ---
    recovered = _recover_truncated_json(text)
    if recovered:
        if settings.scan_debug:
            logger.info(
                f"Recovered {len(recovered['findings'])} finding(s) from truncated Gemini response"
            )
        return _build_summary(recovered), "recovered"

    logger.error("Gemini response unrecoverable — returning empty review")
    return _fallback_review(), "failed"


# ============================================================
#  Fix generation
# ============================================================

def _is_fix_worthy(finding: ReviewComment) -> bool:
    """Return True when a finding should get a dedicated patch-generation pass."""
    lowered = finding.body.lower()
    if finding.fixable is False:
        return False
    if finding.fixable is True:
        if finding.severity in {Severity.LOW, Severity.INFO}:
            return False
        if finding.confidence == "low" and finding.severity == Severity.MEDIUM:
            return False
        return True

    noisy_patterns = (
        "incomplete feature",
        "requires confirmation",
        "needs to be confirmed",
        "placeholder",
        "todo",
        "may be a bug",
    )
    if any(pattern in lowered for pattern in noisy_patterns):
        return False

    if finding.severity in {Severity.CRITICAL, Severity.HIGH}:
        return True

    if finding.severity in {Severity.LOW, Severity.INFO}:
        return False

    strong_fix_patterns = (
        "memory leak",
        "unsubscribe",
        "unsubscription",
        "missing ondestroy",
        "missing null check",
        "null reference",
        "duplicate key",
        "off-by-one",
        "invalidcastexception",
        "argumentoutofrange",
        "returns early instead of continue",
        "double remove",
        "bypassing effect cleanup",
        "leaks gameobject",
        "skipping remaining",
    )
    perf_only_patterns = (
        "gc pressure",
        "console spam",
        "string concatenation",
        "linq allocations",
        "list allocation",
        "allocates list",
        "allocates memory",
        "called in hot path",
    )

    if any(pattern in lowered for pattern in strong_fix_patterns):
        return True
    if any(pattern in lowered for pattern in perf_only_patterns):
        return False

    return finding.severity == Severity.MEDIUM


def _is_high_value_finding(finding: ReviewComment) -> bool:
    """Filter out low-signal findings so final output stays bug-first."""
    lowered = finding.body.lower()
    high_value_categories = {
        "correctness",
        "security",
        "resource",
        "lifecycle",
        "threading",
        "bounds",
        "data-integrity",
        "api-contract",
    }

    if finding.severity in {Severity.CRITICAL, Severity.HIGH}:
        return True

    low_value_patterns = (
        "redundant ternary",
        "return condition directly",
        "redundant count",
        "redundant cast",
        "empty start override",
        "displayname getter allocates new string",
        "active debug.logformat in production code",
        "minor code smell",
    )
    if any(pattern in lowered for pattern in low_value_patterns):
        return False

    if settings.scan_profile == "deep":
        if finding.confidence == "low" and finding.category not in high_value_categories:
            return False
        return finding.severity not in {Severity.LOW, Severity.INFO}

    if finding.category in high_value_categories:
        if finding.confidence == "low" and finding.severity == Severity.MEDIUM:
            return False
        return finding.severity not in {Severity.LOW, Severity.INFO}

    medium_keep_patterns = (
        "memory leak",
        "unsubscribe",
        "null check",
        "nullreference",
        "invalidoperationexception",
        "invalidcastexception",
        "argumentoutofrange",
        "dividebyzero",
        "duplicate key",
        "logic bug",
        "runtime crash",
        "off-by-one",
        "double remove",
        "bypass",
        "skips",
        "stale state",
        "reflection",
        "resistance",
        "fast travel",
        "frame stutters",
        "disk load",
        "resources.load",
    )
    if any(pattern in lowered for pattern in medium_keep_patterns):
        return True

    return finding.severity == Severity.MEDIUM and _is_fix_worthy(finding)


def _filter_findings(review: ReviewSummary) -> ReviewSummary:
    """Keep final findings focused on meaningful bugs and actionable risks."""
    kept = [finding for finding in review.findings if _is_high_value_finding(finding)]
    if len(kept) == len(review.findings):
        return review
    return ReviewSummary(
        summary=review.summary,
        findings=kept,
        critical_count=review.critical_count,
        has_critical=review.has_critical,
        usage=review.usage,
    )


def _is_unavailable_summary(summary: str) -> bool:
    return summary in {"AI review unavailable.", "Split required."}


def _combine_review_summaries(
    summaries: list[str],
    *,
    fallback: str = "AI review unavailable.",
    force_recovered_text: bool = False,
) -> str:
    """Combine summaries while suppressing internal placeholders."""
    informative = [
        summary for summary in summaries
        if summary and not _is_unavailable_summary(summary)
    ]
    if informative:
        return " | ".join(informative)
    if force_recovered_text:
        return "Review completed from split sub-chunks."
    if "Rate limited." in summaries:
        return "Rate limited."
    return fallback


def _fix_priority(finding: ReviewComment) -> tuple[int, int]:
    """Sort findings so the most valuable auto-fixes run first."""
    severity_score = {
        Severity.CRITICAL: 4,
        Severity.HIGH: 3,
        Severity.MEDIUM: 2,
        Severity.LOW: 1,
        Severity.INFO: 0,
    }[finding.severity]
    lowered = finding.body.lower()
    safety_bonus = 0
    if any(token in lowered for token in ("memory leak", "null", "duplicate key", "off-by-one", "unsubscribe")):
        safety_bonus = 2
    elif any(token in lowered for token in ("invalidcast", "argumentoutofrange", "cleanup", "continue")):
        safety_bonus = 1
    return (severity_score, safety_bonus)


def _extract_patch_context(content: str, line: int) -> tuple[str, str]:
    """Return a bounded line window around the finding for patch generation."""
    lines = content.replace("\r\n", "\n").split("\n")
    if not lines:
        return ("whole file", content)

    radius = max(12, settings.patch_context_lines)
    start = max(0, line - 1 - radius)
    end = min(len(lines), line - 1 + radius + 1)
    snippet = "\n".join(lines[start:end])
    return (f"lines {start + 1}-{end}", snippet)


def _extract_patch_context_for_findings(
    content: str,
    findings: list[ReviewComment],
) -> tuple[str, str]:
    """Return one bounded line window covering a local patch set."""
    lines = content.replace("\r\n", "\n").split("\n")
    if not lines or not findings:
        return ("whole file", content)

    radius = max(12, settings.patch_context_lines)
    min_line = min(f.line for f in findings)
    max_line = max((f.end_line or f.line) for f in findings)
    start = max(0, min_line - 1 - radius)
    end = min(len(lines), max_line + radius)
    snippet = "\n".join(lines[start:end])
    return (f"lines {start + 1}-{end}", snippet)


def _group_actionable_findings(findings: list[ReviewComment]) -> list[list[ReviewComment]]:
    """Group findings by file and nearby patch-set so one API call can fix several."""
    if not findings:
        return []

    by_file: dict[str, list[ReviewComment]] = {}
    for finding in findings:
        by_file.setdefault(finding.file, []).append(finding)

    groups: list[list[ReviewComment]] = []
    proximity_threshold = max(20, settings.patch_context_lines // 2)

    for file_findings in by_file.values():
        file_findings.sort(key=lambda item: (item.line, item.end_line or item.line, item.body))
        keyed: dict[str, list[ReviewComment]] = {}
        unkeyed: list[ReviewComment] = []
        for finding in file_findings:
            key = (finding.patch_group_key or "").strip()
            if key:
                keyed.setdefault(key, []).append(finding)
            else:
                unkeyed.append(finding)

        groups.extend(keyed.values())

        current: list[ReviewComment] = []
        current_end = -1
        for finding in unkeyed:
            finding_start = finding.line
            finding_end = finding.end_line or finding.line
            if not current:
                current = [finding]
                current_end = finding_end
                continue
            if (
                finding_start - current_end <= proximity_threshold
                and len(current) < 4
            ):
                current.append(finding)
                current_end = max(current_end, finding_end)
                continue
            groups.append(current)
            current = [finding]
            current_end = finding_end
        if current:
            groups.append(current)

    def _group_sort_key(group: list[ReviewComment]) -> tuple[int, int, str, int]:
        top_priority = max((_fix_priority(item) for item in group), default=(0, 0))
        severity_score, safety_bonus = top_priority
        return (-severity_score, -safety_bonus, group[0].file, group[0].line)

    groups.sort(
        key=_group_sort_key
    )
    return groups


def _parse_fix_response(
    text: str,
    default_findings: list[ReviewComment],
) -> list[tuple[list[int], ReviewComment]]:
    """Parse grouped patch response into indexed edits."""
    try:
        sanitized = _sanitize_json(text)
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(sanitized)
    except (json.JSONDecodeError, TypeError):
        return []

    raw_edits = data.get("edits", [])
    if isinstance(raw_edits, dict):
        raw_edits = [raw_edits]
    if not isinstance(raw_edits, list):
        return []

    parsed: list[tuple[list[int], ReviewComment]] = []
    for item in raw_edits:
        if not isinstance(item, dict):
            continue
        finding_indices = item.get("finding_indices")
        if not isinstance(finding_indices, list) or not finding_indices:
            single_index = item.get("finding_index")
            if isinstance(single_index, int):
                finding_indices = [single_index]
            else:
                continue

        normalized_indices: list[int] = []
        for index in finding_indices:
            if not isinstance(index, int):
                continue
            if 1 <= index <= len(default_findings) and index not in normalized_indices:
                normalized_indices.append(index)
        if not normalized_indices:
            continue

        primary = default_findings[normalized_indices[0] - 1]
        old_code = item.get("old_code", "")
        suggestion = item.get("suggestion", "")
        if not isinstance(old_code, str) or not isinstance(suggestion, str):
            continue
        if not old_code.strip() and not suggestion.strip():
            continue

        parsed.append(
            (
                normalized_indices,
                ReviewComment(
                    file=str(item.get("file", primary.file)),
                    line=int(item.get("line", primary.line)),
                    end_line=item.get("end_line", primary.end_line),
                    severity=primary.severity,
                    body=str(item.get("message", primary.body)),
                    old_code=old_code,
                    suggestion=suggestion,
                ),
            )
        )
    return parsed


def _finding_patch_key(finding: ReviewComment) -> tuple[str, int, str]:
    """Stable key for attaching generated edits back to original findings."""
    return (finding.file, finding.line, finding.body)


async def _generate_fixes_for_group(
    findings: list[ReviewComment],
    repo_file_map: dict[str, str],
    issue_seeds: list[IssueSeed] | None = None,
    code_index=None,
    comments: bool = False,
    on_progress: ProgressFn = None,
    usage: UsageStats | None = None,
    model_chain: _ModelChain | None = None,
    label: str = "",
) -> dict[tuple[str, int, str], ReviewComment]:
    """Generate structured edits for one local patch-set."""
    if not findings:
        return {}

    target_file = findings[0].file
    target_content = repo_file_map.get(target_file)
    if target_content is None:
        return {}
    target_context_label, target_context = _extract_patch_context_for_findings(target_content, findings)

    min_line = min(finding.line for finding in findings)
    max_line = max((finding.end_line or finding.line) for finding in findings)
    matching_seeds: list[IssueSeed] = []
    if issue_seeds is not None:
        for seed in issue_seeds:
            if seed.file != target_file:
                continue
            if min_line - 8 <= seed.line <= max_line + 8:
                matching_seeds.append(seed)

    related_snippets = []

    if code_index is not None:
        from ..context.retriever import retrieve_related_context

        retrieval = retrieve_related_context(
            primary_files=[target_file],
            repo_file_map=repo_file_map,
            code_index=code_index,
            issue_seeds=matching_seeds or None,
        )
        related_snippets = retrieval.snippets[:2]

    selected_pack_ids = select_prompt_pack_ids(
        [(target_file, target_context)],
        issue_seeds=[*findings, *matching_seeds],
        stage="fix",
        max_extra_packs=settings.prompt_pack_fix_max_extras,
    )

    system_prompt = build_fix_system_prompt(
        comments=comments,
        selected_pack_ids=selected_pack_ids,
    )
    user_message = build_fix_user_message(
        findings=findings,
        static_hints=matching_seeds,
        target_file=target_file,
        target_content=target_context,
        target_context_label=target_context_label,
        related_snippets=related_snippets,
    )
    output_tokens = settings.patch_output_tokens
    model_name = model_chain.current if model_chain else settings.patch_llm_model
    url = model_chain.build_url() if model_chain else (
        f"{settings.gemini_base_url}/{settings.patch_llm_model}"
        f":generateContent?key={settings.google_api_key}"
    )
    effective_timeout = settings.patch_timeout

    if settings.scan_debug and on_progress:
        on_progress(
            f"  {label} patch - prompt chars: system={len(system_prompt):,}, "
            f"user={len(user_message):,}, packs={','.join(selected_pack_ids)}, "
            f"model={model_chain.current if model_chain else settings.patch_llm_model}"
        )

    try:
        response = await _with_heartbeat(
            asyncio.wait_for(
                _gemini_post(url, {
                    "system_instruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_message}]}],
                    "generationConfig": _build_generation_config(
                        model_name,
                        output_tokens,
                        response_json_schema=build_fix_response_schema(),
                    ),
                }, timeout=effective_timeout),
                timeout=effective_timeout,
            ),
            emit=on_progress,
            label=f"{label} patch" if label else "Generating patch",
        )
    except asyncio.TimeoutError:
        return {}

    if response is None:
        return {}
    if response.status_code == 429 and model_chain:
        failed_model = model_chain.current
        next_model = await model_chain.advance(failed_model)
        if next_model:
            if on_progress:
                on_progress(f"  {label} patch - rate limited on {failed_model}, switching to {next_model}")
            return await _generate_fixes_for_group(
                findings=findings,
                repo_file_map=repo_file_map,
                issue_seeds=issue_seeds,
                code_index=code_index,
                comments=comments,
                on_progress=on_progress,
                usage=usage,
                model_chain=model_chain,
                label=label,
            )
        return {}
    if response.status_code != 200:
        return {}

    result = response.json()
    if usage:
        usage.add(
            result,
            model_name,
            stage="patch",
            request_label=label or target_file,
            extra={
                "target_file": target_file,
                "finding_count": len(findings),
                "finding_lines": [finding.line for finding in findings],
                "patch_group_keys": [finding.patch_group_key or "" for finding in findings],
                "selected_pack_ids": selected_pack_ids,
                "system_prompt_chars": len(system_prompt),
                "user_message_chars": len(user_message),
                "target_context_chars": len(target_context),
                "related_snippet_count": len(related_snippets),
            },
        )
    candidates = result.get("candidates") or []
    if not candidates:
        return {}
    try:
        text = candidates[0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return {}

    patch_index: dict[tuple[str, int, str], ReviewComment] = {}
    for finding_indices, patch in _parse_fix_response(text, findings):
        for index in finding_indices:
            original = findings[index - 1]
            patch_index.setdefault(_finding_patch_key(original), patch)

    return patch_index


async def _attach_fixes_for_chunk(
    review: ReviewSummary,
    repo_file_map: dict[str, str],
    issue_seeds: list[IssueSeed] | None = None,
    code_index=None,
    comments: bool = False,
    on_progress: ProgressFn = None,
    usage: UsageStats | None = None,
    chunk_label: str = "",
) -> ReviewSummary:
    """Generate patch suggestions in a second pass for actionable findings."""
    if not review.findings:
        return review

    actionable = [finding for finding in review.findings if _is_fix_worthy(finding)]
    if not actionable:
        return review

    grouped_actionable = _group_actionable_findings(actionable)
    selected_groups: list[list[ReviewComment]] = []
    selected_count = 0
    for group in grouped_actionable:
        group_size = len(group)
        if selected_groups and selected_count + group_size > settings.patch_max_findings_per_chunk:
            break
        selected_groups.append(group)
        selected_count += group_size

    if not selected_groups:
        return review

    if on_progress and selected_count > 1:
        on_progress(
            f"  {chunk_label} patching {selected_count} finding(s) across "
            f"{len(selected_groups)} patch set(s) "
            f"[{_compute_patch_concurrency(settings.patch_llm_model, len(selected_groups))} concurrent]"
        )

    patch_index: dict[tuple[str, int, str], ReviewComment] = {}
    patch_concurrency = _compute_patch_concurrency(settings.patch_llm_model, len(selected_groups))
    sem = asyncio.Semaphore(patch_concurrency)

    async def _run_patch(idx: int, group: list[ReviewComment]) -> dict[tuple[str, int, str], ReviewComment]:
        async with sem:
            label = f"{chunk_label} patch-set {idx}/{len(selected_groups)}".strip()
            return await _generate_fixes_for_group(
                findings=group,
                repo_file_map=repo_file_map,
                issue_seeds=issue_seeds,
                code_index=code_index,
                comments=comments,
                on_progress=on_progress,
                usage=usage,
                model_chain=_ModelChain(settings.patch_llm_model, models=_PATCH_FALLBACK_MODELS),
                label=label,
            )

    patch_maps = await asyncio.gather(
        *[_run_patch(idx, group) for idx, group in enumerate(selected_groups, 1)]
    )
    for patch_map in patch_maps:
        patch_index.update(patch_map)

    updated: list[ReviewComment] = []
    for finding in review.findings:
        patch = patch_index.get(_finding_patch_key(finding))
        if patch is None:
            updated.append(finding)
            continue
        updated.append(
            ReviewComment(
                file=finding.file,
                line=finding.line,
                end_line=patch.end_line or finding.end_line,
                severity=finding.severity,
                body=finding.body,
                category=finding.category,
                confidence=finding.confidence,
                fixable=finding.fixable,
                patch_group_key=finding.patch_group_key,
                old_code=patch.old_code,
                suggestion=patch.suggestion,
            )
        )

    return ReviewSummary(
        summary=review.summary,
        findings=[finding for finding in updated if _is_high_value_finding(finding)],
        critical_count=review.critical_count,
        has_critical=review.has_critical,
        usage=review.usage,
    )


# ============================================================
#  Full-codebase scan
# ============================================================

def _build_repo_map(file_contents: list[tuple[str, str]]) -> str:
    """Build a directory-level summary for LLM planning.

    Output: one line per directory with file count and total size.
    Gemini groups directories, not individual files — keeps I/O small.

      Assets/Scripts/Game/ (45 files, 890K)
      Assets/Scripts/API/ (28 files, 320K)
    """
    import posixpath
    from collections import defaultdict

    dirs: dict[str, list[int]] = defaultdict(list)
    for path, content in file_contents:
        norm = path.replace("\\", "/")
        folder = posixpath.dirname(norm) or "."
        dirs[folder].append(len(content))

    lines_out: list[str] = []
    # Sort by total chars descending — largest directories first
    for folder, sizes in sorted(dirs.items(), key=lambda x: -sum(x[1])):
        total = sum(sizes)
        size_str = f"{total / 1000:.0f}K" if total >= 1000 else str(total)
        lines_out.append(f"{folder}/ ({len(sizes)} files, {size_str})")

    return "\n".join(lines_out)


async def _plan_chunks(
    file_contents: list[tuple[str, str]],
    detected_languages: list[str],
    char_budget: int,
    on_progress: ProgressFn = None,
    model_chain: _ModelChain | None = None,
    usage: UsageStats | None = None,
) -> list[list[tuple[str, str]]] | None:
    """Ask Gemini to plan optimal file groupings. Returns None on failure."""
    def _emit(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    repo_map = _build_repo_map(file_contents)
    total_files = len(file_contents)
    total_chars = sum(len(c) for _, c in file_contents)

    system_prompt = build_plan_prompt(char_budget)
    user_message = build_plan_user_message(repo_map, total_files, total_chars)

    url = model_chain.build_url() if model_chain else (
        f"{settings.gemini_base_url}/{settings.llm_model}"
        f":generateContent?key={settings.google_api_key}"
    )

    def _on_retry(attempt: int, msg: str) -> None:
        _emit(f"  Attempt {attempt} failed: {msg}")

    try:
        response = await _with_heartbeat(
            asyncio.wait_for(
                _gemini_post(url, {
                    "system_instruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_message}]}],
                    "generationConfig": _build_generation_config(
                        model_chain.current if model_chain else settings.llm_model,
                        settings.plan_output_tokens,
                        response_json_schema=build_plan_response_schema(),
                    ),
                }, timeout=settings.plan_timeout, on_retry=_on_retry),
                timeout=settings.plan_timeout,
            ),
            emit=on_progress, label="Planning",
        )
    except asyncio.TimeoutError:
        _emit(f"  Planning hard timeout at {int(settings.plan_timeout)}s, using greedy chunking")
        return None

    if response is not None and response.status_code == 429 and model_chain:
        failed = model_chain.current
        next_model = await model_chain.advance(failed)
        if next_model:
            _emit(f"  Planning rate limited on {failed}, retrying with {next_model}")
            return await _plan_chunks(
                file_contents, detected_languages, char_budget,
                on_progress=on_progress, model_chain=model_chain,
                usage=usage,
            )

    if response is None or response.status_code != 200:
        status = f" (HTTP {response.status_code})" if response else ""
        _emit(f"  Planning failed{status}, using greedy chunking")
        return None

    try:
        result = response.json()
        if usage:
            usage.add(
                result,
                model_chain.current if model_chain else settings.llm_model,
                stage="plan",
                request_label="planning",
                extra={
                    "file_count": total_files,
                    "total_chars": total_chars,
                    "char_budget": char_budget,
                    "repo_map_chars": len(repo_map),
                    "system_prompt_chars": len(system_prompt),
                    "user_message_chars": len(user_message),
                },
            )
        text = result["candidates"][0]["content"]["parts"][0]["text"]
        sanitized = _sanitize_json(text)
        data = json.loads(sanitized)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logger.warning(f"Failed to parse planning response: {e}")
        return None

    # Build dir -> files lookup for expanding directory assignments
    import posixpath
    from collections import defaultdict

    dir_files: dict[str, list[tuple[str, str]]] = defaultdict(list)
    dir_files_lower: dict[str, str] = {}  # lowercase -> canonical dir key
    for path, content in file_contents:
        norm = path.replace("\\", "/")
        folder = posixpath.dirname(norm) or "."
        dir_key = folder + "/"
        dir_files[dir_key].append((path, content))
        dir_files_lower[dir_key.lower()] = dir_key

    def _resolve_dir(raw: str) -> str | None:
        """Resolve a Gemini-returned directory to a dir_files key."""
        norm = raw.replace("\\", "/").rstrip("/") + "/"
        if norm.startswith("./") and norm != "./":
            norm = norm[2:]
        if norm in dir_files:
            return norm
        lower = norm.lower()
        if lower in dir_files_lower:
            return dir_files_lower[lower]
        return None

    # Validate and build chunks from directory assignments
    planned_chunks: list[list[tuple[str, str]]] = []
    seen_dirs: set[str] = set()

    for chunk_def in data.get("chunks", []):
        chunk_files: list[tuple[str, str]] = []
        for dir_path in chunk_def.get("dirs", []):
            key = _resolve_dir(dir_path)
            if key is None:
                logger.warning(f"Planning: unknown dir '{dir_path}', skipping")
                continue
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            chunk_files.extend(dir_files[key])
        if chunk_files:
            planned_chunks.append(chunk_files)

    # Check coverage — every directory must be assigned
    missing_dirs = set(dir_files.keys()) - seen_dirs
    missing_files: list[tuple[str, str]] = []
    for d in sorted(missing_dirs):
        missing_files.extend(dir_files[d])
    if missing_files:
        _emit(f"  Planning missed {len(missing_dirs)} dir(s) ({len(missing_files)} files), appending as extra chunk(s)")
        planned_chunks.append(missing_files)

    if not planned_chunks:
        _emit("  Planning returned empty chunks, using greedy chunking")
        return None

    # Post-process: split any oversized chunk through _chunk_files()
    # LLM often ignores char budget when dirs are individually large
    final_chunks: list[list[tuple[str, str]]] = []
    focus_list = [c.get("focus", "") for c in data.get("chunks", [])]
    effective_budget = _effective_scan_char_budget(char_budget)
    for i, ch in enumerate(planned_chunks):
        chars = sum(len(c) for _, c in ch)
        focus = f" — {focus_list[i]}" if i < len(focus_list) and focus_list[i] else ""
        if chars > effective_budget or len(ch) > settings.scan_max_files_per_chunk:
            sub = _chunk_files(ch, char_budget)
            _emit(f"  Chunk {i+1}: {len(ch)} files, {chars:,} chars → split into {len(sub)} sub-chunks{focus}")
            final_chunks.extend(sub)
        else:
            _emit(f"  Chunk {i+1}: {len(ch)} files, {chars:,} chars{focus}")
            final_chunks.append(ch)
    return final_chunks


def _chunk_files(
    file_contents: list[tuple[str, str]],
    char_budget: int,
) -> list[list[tuple[str, str]]]:
    """Split files into chunks that fit within the character budget."""
    effective_budget = _effective_scan_char_budget(char_budget)
    chunks: list[list[tuple[str, str]]] = []
    current_chunk: list[tuple[str, str]] = []
    current_size = 0

    # Sort largest first — complex files get maximum LLM attention
    sorted_files = sorted(file_contents, key=lambda x: len(x[1]), reverse=True)

    for path, content in sorted_files:
        file_size = len(content) + len(path) + 20  # header overhead
        needs_split = (
            current_chunk and (
                current_size + file_size > effective_budget
                or len(current_chunk) >= settings.scan_max_files_per_chunk
            )
        )
        if needs_split:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0
        current_chunk.append((path, content))
        current_size += file_size

    if current_chunk:
        chunks.append(current_chunk)

    return chunks if chunks else [[]]


def _build_numbered_section_content(lines: list[str], start: int, end: int) -> str:
    """Build a numbered line window so line references stay meaningful."""
    numbered = [
        f"{line_no:5d}: {line}"
        for line_no, line in enumerate(lines[start:end], start=start + 1)
    ]
    return "\n".join(numbered)


def _split_single_file_into_sections(
    path: str,
    content: str,
    *,
    target_chars: int = 28_000,
    overlap_lines: int = 30,
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Split one large file into overlapping numbered sections."""
    lines = content.replace("\r\n", "\n").split("\n")
    if not lines:
        return [(f"{path}:1-1", [(path, content)])]

    sections: list[tuple[str, list[tuple[str, str]]]] = []
    start = 0
    total = len(lines)

    while start < total:
        end = start
        chars = 0
        while end < total:
            candidate = lines[end]
            line_cost = len(candidate) + 12
            if end > start and chars + line_cost > target_chars:
                break
            chars += line_cost
            end += 1

        if end <= start:
            end = min(total, start + 1)

        label = f"{path}:{start + 1}-{end}"
        section_text = _build_numbered_section_content(lines, start, end)
        sections.append((label, [(path, section_text)]))

        if end >= total:
            break
        start = max(start + 1, end - overlap_lines)

    return sections


async def _scan_chunk(
    file_contents: list[tuple[str, str]],
    detected_languages: list[str],
    chunk_info: str = "",
    comments: bool = False,
    on_progress: ProgressFn = None,
    model_chain: _ModelChain | None = None,
    usage: UsageStats | None = None,
    repo_file_map: dict[str, str] | None = None,
    issue_seeds: list[IssueSeed] | None = None,
    code_index=None,
    parse_retries_remaining: int = 1,
    max_snippets_override: int | None = None,
    max_snippet_chars_override: int | None = None,
    issue_seed_limit: int | None = None,
    output_tokens_override: int | None = None,
    degraded_mode: bool = False,
) -> ReviewSummary:
    """Send a single chunk of files to Gemini for scan review."""
    related_snippets = []
    chunk_issue_seeds = []
    if repo_file_map is not None:
        from ..context.retriever import retrieve_related_context

        max_snippets = 8
        max_snippet_chars = 12000
        if len(file_contents) >= 18:
            max_snippets = 3
            max_snippet_chars = 3500
        elif len(file_contents) >= 10:
            max_snippets = 5
            max_snippet_chars = 6000

        if len(file_contents) == 1 and sum(len(content) for _, content in file_contents) >= 45_000:
            max_snippets = min(max_snippets, 2)
            max_snippet_chars = min(max_snippet_chars, 2500)

        if max_snippets_override is not None:
            max_snippets = max_snippets_override
        if max_snippet_chars_override is not None:
            max_snippet_chars = max_snippet_chars_override

        if max_snippets > 0 or issue_seed_limit != 0:
            retrieval = retrieve_related_context(
                primary_files=[path for path, _ in file_contents],
                repo_file_map=repo_file_map,
                code_index=code_index,
                issue_seeds=issue_seeds,
                max_snippets=max_snippets,
                max_snippet_chars=max_snippet_chars,
            )
            related_snippets = retrieval.snippets[:max_snippets] if max_snippets >= 0 else retrieval.snippets
            chunk_issue_seeds = retrieval.issue_seeds
            if issue_seed_limit is not None:
                chunk_issue_seeds = chunk_issue_seeds[:issue_seed_limit]

    selected_pack_ids = select_prompt_pack_ids(
        file_contents,
        issue_seeds=chunk_issue_seeds,
        stage="scan",
        max_extra_packs=settings.prompt_pack_scan_max_extras,
    )

    system_prompt = build_scan_system_prompt(
        detected_languages,
        comments=comments,
        selected_pack_ids=selected_pack_ids,
    )

    user_message = build_scan_user_message(
        file_contents=file_contents,
        detected_languages=detected_languages,
        chunk_info=chunk_info,
        issue_seeds=chunk_issue_seeds,
        related_snippets=related_snippets,
    )
    chunk_chars = sum(len(content) for _, content in file_contents)
    requested_output_tokens = _compute_scan_output_tokens(
        chunk_chars,
        len(file_contents),
        len(chunk_issue_seeds),
    )
    if output_tokens_override is not None:
        requested_output_tokens = output_tokens_override

    model_name = model_chain.current if model_chain else settings.llm_model
    output_tokens = _normalize_scan_output_tokens(model_name, requested_output_tokens)
    generation_config = _build_generation_config(
        model_name,
        output_tokens,
        response_json_schema=build_scan_response_schema(),
    )

    if settings.scan_debug and on_progress:
        on_progress(
            f"  {chunk_info} - prompt chars: "
            f"system={len(system_prompt):,}, user={len(user_message):,}, "
            f"chunk={chunk_chars:,}, files={len(file_contents)}, "
            f"issue_seeds={len(chunk_issue_seeds)}, snippets={len(related_snippets)}, "
            f"packs={','.join(selected_pack_ids)}, requested_output_tokens={requested_output_tokens:,}, "
            f"effective_output_tokens={output_tokens:,}, "
            f"model={model_name}"
        )

    url = model_chain.build_url() if model_chain else (
        f"{settings.gemini_base_url}/{settings.llm_model}"
        f":generateContent?key={settings.google_api_key}"
    )

    def _on_retry(attempt: int, msg: str) -> None:
        if on_progress:
            on_progress(f"  {chunk_info} — attempt {attempt} failed: {msg}")

    # Give single-file chunks 2x timeout — they can't be split further
    effective_timeout = _compute_effective_timeout(chunk_chars, len(file_contents))

    try:
        response = await _with_heartbeat(
            asyncio.wait_for(
                _gemini_post(url, {
                    "system_instruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_message}]}],
                    "generationConfig": generation_config,
                }, timeout=effective_timeout, on_retry=_on_retry),
                timeout=effective_timeout,
            ),
            emit=on_progress, label=chunk_info or "Reviewing",
        )
    except asyncio.TimeoutError:
        if on_progress:
            on_progress(f"  {chunk_info} — hard timeout at {int(effective_timeout)}s")
        return _fallback_review()

    if response is None:
        return _fallback_review()

    if response.status_code == 429:
        if model_chain:
            failed = model_chain.current
            next_model = await model_chain.advance(failed)
            if next_model:
                if on_progress:
                    on_progress(f"  {chunk_info} — rate limited on {failed}, switching to {next_model}")
                return await _scan_chunk(
                    file_contents, detected_languages, chunk_info,
                    comments=comments,
                    on_progress=on_progress, model_chain=model_chain,
                    usage=usage,
                    repo_file_map=repo_file_map,
                    issue_seeds=issue_seeds,
                    code_index=code_index,
                    parse_retries_remaining=parse_retries_remaining,
                )
        logger.error("Rate limited (429) — no fallback models left")
        return _rate_limited_review()

    if response.status_code != 200:
        logger.error(f"Gemini API error {response.status_code}: {response.text[:500]}")
        return _fallback_review()

    result = response.json()
    if usage:
        usage.add(
            result,
            model_name,
            stage="scan",
            request_label=chunk_info,
            extra={
                "chunk_file_count": len(file_contents),
                "chunk_chars": chunk_chars,
                "issue_seed_count": len(chunk_issue_seeds),
                "related_snippet_count": len(related_snippets),
                "selected_pack_ids": selected_pack_ids,
                "system_prompt_chars": len(system_prompt),
                "user_message_chars": len(user_message),
                "requested_output_tokens": requested_output_tokens,
                "max_output_tokens": output_tokens,
                "thinking_config": generation_config.get("thinkingConfig"),
                "degraded_mode": degraded_mode,
                "parse_retries_remaining": parse_retries_remaining,
            },
        )

    if "error" in result:
        logger.error(f"Gemini API error: {result['error'].get('message', 'Unknown')}")
        return _fallback_review()

    if "candidates" not in result or not result["candidates"]:
        logger.error(f"Malformed Gemini response: {json.dumps(result)[:500]}")
        return _fallback_review()

    candidate = result["candidates"][0]
    if "content" not in candidate or "parts" not in candidate["content"]:
        logger.error(f"Malformed candidate: {json.dumps(candidate)[:500]}")
        return _fallback_review()

    finish_reason = str(candidate.get("finishReason", "") or "")
    finish_message = str(candidate.get("finishMessage", "") or "")
    text = candidate["content"]["parts"][0]["text"]
    parsed, parse_status = _parse_review_response(text)
    if usage:
        usage.annotate_last_event(
            stage="scan",
            request_label=chunk_info,
            extra={
                "candidate_finish_reason": finish_reason,
                "candidate_finish_message": finish_message,
                "parse_status": parse_status,
                "candidate_text_chars": len(text),
                "recovered_findings_count": len(parsed.findings) if parse_status == "recovered" else 0,
            },
        )
    if settings.scan_debug and parse_status in {"failed", "recovered"} and on_progress:
        reason_bits = []
        if finish_reason:
            reason_bits.append(f"finishReason={finish_reason}")
        if finish_message:
            reason_bits.append(f"finishMessage={finish_message}")
        if reason_bits:
            on_progress(f"  {chunk_info} - candidate stopped with {'; '.join(reason_bits)}")
    if parse_status in {"failed", "recovered"} and parse_retries_remaining > 0:
        should_split_large_chunk = (
            len(file_contents) > 4
            or chunk_chars > 45_000
            or len(user_message) > 60_000
        )
        if len(file_contents) == 1 and should_split_large_chunk:
            path, content = file_contents[0]
            reason = "partial JSON recovered" if parse_status == "recovered" else "malformed JSON response"

            if not degraded_mode:
                if on_progress:
                    on_progress(f"  {chunk_info} - {reason}, retrying single file with reduced context")
                return await _scan_chunk(
                    file_contents,
                    detected_languages,
                    chunk_info,
                    comments=comments,
                    on_progress=on_progress,
                    model_chain=model_chain,
                    usage=usage,
                    repo_file_map=repo_file_map,
                    issue_seeds=issue_seeds,
                    code_index=code_index,
                    parse_retries_remaining=parse_retries_remaining - 1,
                    max_snippets_override=2,
                    max_snippet_chars_override=2200,
                    issue_seed_limit=2,
                    output_tokens_override=4096,
                    degraded_mode=True,
                )

            if parse_status == "recovered" and parsed.findings:
                if on_progress:
                    on_progress(f"  {chunk_info} - using {len(parsed.findings)} recovered finding(s) from partial JSON")
                return _filter_findings(parsed)

            sections = _split_single_file_into_sections(path, content)
            if len(sections) > 1:
                if on_progress:
                    on_progress(
                        f"  {chunk_info} - {reason}, splitting single file into {len(sections)} line-window sections"
                    )
                section_results: list[ReviewSummary] = []
                for index, (section_label, section_files) in enumerate(sections, 1):
                    section_result = await _scan_chunk(
                        section_files,
                        detected_languages,
                        f"{chunk_info}/s{index}",
                        comments=comments,
                        on_progress=on_progress,
                        model_chain=_ModelChain(model_chain.current if model_chain else settings.llm_model),
                        usage=usage,
                        repo_file_map=repo_file_map,
                        issue_seeds=issue_seeds,
                        code_index=code_index,
                        parse_retries_remaining=0,
                        max_snippets_override=1,
                        max_snippet_chars_override=1600,
                        issue_seed_limit=1,
                        output_tokens_override=4096,
                        degraded_mode=True,
                    )
                    section_results.append(section_result)

                deduped: list[ReviewComment] = []
                seen_keys: set[tuple[str, int, str]] = set()
                for result in section_results:
                    for finding in result.findings:
                        key = (finding.file, finding.line, finding.body)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        deduped.append(finding)

                total_critical = sum(result.critical_count for result in section_results)
                merged_summary = _combine_review_summaries(
                    [result.summary for result in section_results],
                    fallback="AI review unavailable.",
                    force_recovered_text=bool(deduped),
                )
                return ReviewSummary(
                    summary=merged_summary,
                    findings=deduped,
                    critical_count=total_critical,
                    has_critical=total_critical > 0,
                )

            if on_progress:
                on_progress(f"  {chunk_info} - {reason}, single-file recovery exhausted")
            return _fallback_review()

        if should_split_large_chunk:
            if on_progress:
                reason = "partial JSON recovered" if parse_status == "recovered" else "malformed JSON response"
                on_progress(f"  {chunk_info} - {reason}, returning split signal for large chunk")
            return _split_required_review()
        if (
            finish_reason == "MAX_TOKENS"
            and _is_gemini3_family(model_name)
            and _is_pro_model(model_name)
        ):
            retry_output_tokens = min(
                settings.scan_output_tokens,
                max(output_tokens * 2, settings.scan_pro_min_output_tokens),
            )
            if retry_output_tokens > output_tokens:
                if on_progress:
                    on_progress(
                        f"  {chunk_info} - hit MAX_TOKENS, retrying once with higher output cap "
                        f"({retry_output_tokens:,})"
                    )
                return await _scan_chunk(
                    file_contents, detected_languages, chunk_info,
                    comments=comments,
                    on_progress=on_progress, model_chain=model_chain,
                    usage=usage,
                    repo_file_map=repo_file_map,
                    issue_seeds=issue_seeds,
                    code_index=code_index,
                    parse_retries_remaining=parse_retries_remaining - 1,
                    max_snippets_override=max_snippets_override,
                    max_snippet_chars_override=max_snippet_chars_override,
                    issue_seed_limit=issue_seed_limit,
                    output_tokens_override=retry_output_tokens,
                    degraded_mode=degraded_mode,
                )
        retry_note = ""
        if model_chain:
            failed_model = model_chain.current
            next_model = await model_chain.advance(failed_model)
            if next_model:
                retry_note = f", switching to {next_model}"
        if on_progress:
            reason = "partial JSON recovered" if parse_status == "recovered" else "malformed JSON response"
            on_progress(f"  {chunk_info} - {reason}, retrying once{retry_note}")
        return await _scan_chunk(
            file_contents, detected_languages, chunk_info,
            comments=comments,
            on_progress=on_progress, model_chain=model_chain,
            usage=usage,
            repo_file_map=repo_file_map,
            issue_seeds=issue_seeds,
            code_index=code_index,
            parse_retries_remaining=parse_retries_remaining - 1,
        )
    return _filter_findings(parsed)


async def scan_with_gemini(
    file_contents: list[tuple[str, str]],
    detected_languages: list[str] | None = None,
    comments: bool = False,
    on_progress: Callable[[str], None] | None = None,
    on_chunk_done: Callable[[ReviewSummary], None] | None = None,
    issue_seeds: list[IssueSeed] | None = None,
    code_index=None,
) -> ReviewSummary:
    """Send full file contents + static findings to Gemini for codebase scan.

    on_chunk_done is called with each chunk's ReviewSummary as soon as it
    completes for progress/reporting hooks.
    """
    if detected_languages is None:
        detected_languages = []

    def _emit(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    plan_model_chain = _ModelChain(settings.llm_model)
    usage = UsageStats()
    repo_file_map = {path: content for path, content in file_contents}

    # Always try LLM planning first — hard timeout protects us
    _emit("Planning review chunks...")
    chunks = await _plan_chunks(
        file_contents, detected_languages, settings.max_scan_chars,
        on_progress=on_progress, model_chain=plan_model_chain, usage=usage,
    )
    if chunks is None:
        _emit("Using greedy chunking")
        chunks = _chunk_files(file_contents, settings.max_scan_chars)
    _emit(f"{len(chunks)} chunk(s) planned, model: {plan_model_chain.current}")

    if len(chunks) == 1:
        chars = sum(len(c) for _, c in chunks[0])
        _emit(f"Reviewing 1 chunk ({len(chunks[0])} files, {chars:,} chars)...")

        # Use same split-on-fail logic as multi-chunk path
        async def _review_single(
            chunk: list[tuple[str, str]], label: str,
        ) -> ReviewSummary:
            chunk_model_chain = _ModelChain(settings.llm_model)
            result = await _scan_chunk(
                chunk, detected_languages, label,
                comments=comments,
                on_progress=on_progress, model_chain=chunk_model_chain,
                usage=usage,
                repo_file_map=repo_file_map,
                issue_seeds=issue_seeds,
                code_index=code_index,
            )
            # 429 — stop immediately, don't split
            if result.summary == "Rate limited.":
                _emit("Rate limited — aborting")
                return result
            if _is_unavailable_summary(result.summary) and len(chunk) > 1:
                mid = len(chunk) // 2
                _emit(f"{label} failed — splitting and retrying")
                a = await _review_single(chunk[:mid], f"{label}/a")
                b = await _review_single(chunk[mid:], f"{label}/b")
                merged = list(a.findings) + list(b.findings)
                crit = a.critical_count + b.critical_count
                return ReviewSummary(
                    summary=_combine_review_summaries(
                        [a.summary, b.summary],
                        force_recovered_text=bool(merged),
                    ),
                    findings=merged, critical_count=crit, has_critical=crit > 0,
                    usage=usage,
                )
            attached = await _attach_fixes_for_chunk(
                result,
                repo_file_map=repo_file_map,
                issue_seeds=issue_seeds,
                code_index=code_index,
                comments=comments,
                on_progress=on_progress,
                usage=usage,
                chunk_label=label,
            )
            _emit(f"{label} completed")
            return attached

        result = await _review_single(chunks[0], "Chunk 1/1")
        result.usage = usage
        _emit(f"Done — {len(result.findings)} issues found")
        if on_chunk_done:
            on_chunk_done(result)
        return result

    # Multiple chunks — call Gemini concurrently, merge results
    max_concurrent = _compute_scan_concurrency(settings.llm_model, len(chunks))
    sem = asyncio.Semaphore(max_concurrent)
    stop_event = asyncio.Event()  # set on 429 — stops all remaining chunks
    completed = 0
    _emit(
        f"Reviewing code [{len(chunks)} chunks, {max_concurrent} concurrent, "
        f"model={settings.llm_model}]"
    )

    async def _review_chunk(
        chunk: list[tuple[str, str]], label: str,
    ) -> ReviewSummary:
        """Scan a chunk; on failure, split in half and retry sub-chunks."""
        if stop_event.is_set():
            return _rate_limited_review()
        chars = sum(len(c) for _, c in chunk)
        _emit(f"{label} {len(chunk)} files, {chars:,} chars...")
        chunk_model_chain = _ModelChain(settings.llm_model)
        result = await _scan_chunk(
            chunk, detected_languages, label,
            comments=comments,
            on_progress=on_progress, model_chain=chunk_model_chain,
            usage=usage,
            repo_file_map=repo_file_map,
            issue_seeds=issue_seeds,
            code_index=code_index,
        )
        # 429 with all models exhausted — stop everything, don't split
        if result.summary == "Rate limited.":
            if not stop_event.is_set():
                _emit("All models rate limited — stopping remaining chunks")
                stop_event.set()
            return result
        # If failed and chunk is splittable, split in half and retry
        if _is_unavailable_summary(result.summary) and len(chunk) > 1:
            mid = len(chunk) // 2
            _emit(f"{label} failed — splitting into 2 sub-chunks and retrying")
            sub_a = await _review_chunk(chunk[:mid], f"{label}/a")
            sub_b = await _review_chunk(chunk[mid:], f"{label}/b")
            merged_findings = list(sub_a.findings) + list(sub_b.findings)
            merged_crit = sub_a.critical_count + sub_b.critical_count
            return ReviewSummary(
                summary=_combine_review_summaries(
                    [sub_a.summary, sub_b.summary],
                    force_recovered_text=bool(merged_findings),
                ),
                findings=merged_findings,
                critical_count=merged_crit,
                has_critical=merged_crit > 0,
                usage=usage,
            )
        attached = await _attach_fixes_for_chunk(
            result,
            repo_file_map=repo_file_map,
            issue_seeds=issue_seeds,
            code_index=code_index,
            comments=comments,
            on_progress=on_progress,
            usage=usage,
            chunk_label=label,
        )
        _emit(f"{label} completed")
        return attached

    async def _scan_with_sem(
        chunk: list[tuple[str, str]], idx: int,
    ) -> ReviewSummary:
        nonlocal completed
        async with sem:
            if stop_event.is_set():
                return _rate_limited_review()
            label = f"[{idx}/{len(chunks)}]"
            result = await _review_chunk(chunk, label)
            completed += 1
            _emit(f"[{idx}/{len(chunks)}] done — "
                  f"{len(result.findings)} issues ({completed}/{len(chunks)} complete)")
            if on_chunk_done:
                on_chunk_done(result)
            return result

    tasks = [
        _scan_with_sem(chunk, i)
        for i, chunk in enumerate(chunks, 1)
    ]
    results = await asyncio.gather(*tasks)

    all_findings: list[ReviewComment] = []
    summaries: list[str] = []
    total_critical = 0
    for result in results:
        all_findings.extend(result.findings)
        summaries.append(result.summary)
        total_critical += result.critical_count

    combined_summary = _combine_review_summaries(
        summaries,
        fallback="AI review unavailable." if not all_findings else "Review completed.",
        force_recovered_text=bool(all_findings),
    )
    return ReviewSummary(
        summary=combined_summary,
        findings=all_findings,
        critical_count=total_critical,
        has_critical=total_critical > 0,
        usage=usage,
    )


def _fallback_review() -> ReviewSummary:
    """Return an empty review when Gemini fails."""
    return ReviewSummary(
        summary="AI review unavailable.",
        findings=[],
        critical_count=0,
        has_critical=False,
    )


def _split_required_review() -> ReviewSummary:
    """Internal sentinel used when a chunk should be recursively split."""
    return ReviewSummary(
        summary="Split required.",
        findings=[],
        critical_count=0,
        has_critical=False,
    )


def _rate_limited_review() -> ReviewSummary:
    """Return an empty review for 429 rate limit — must NOT trigger auto-split."""
    return ReviewSummary(
        summary="Rate limited.",
        findings=[],
        critical_count=0,
        has_critical=False,
    )
