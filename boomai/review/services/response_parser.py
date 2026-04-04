from __future__ import annotations

import json
import logging
import re
from typing import Callable

from ...core.models import ReviewComment, ReviewSummary, Severity


def sanitize_json(text: str) -> str:
    """Fix common Gemini JSON issues: trailing commas."""
    return re.sub(r',\s*([}\]])', r"\1", text)


def recover_truncated_json(text: str) -> dict | None:
    """Recover partial findings from truncated Gemini JSON."""
    summary = "Review completed (output truncated)."
    summary_match = re.search(r'"summary"\s*:\s*("(?:[^"\\]|\\.)*")', text)
    if summary_match:
        try:
            summary = json.loads(summary_match.group(1))
        except json.JSONDecodeError:
            pass

    findings_array_match = re.search(r'"findings"\s*:\s*\[', text)
    if not findings_array_match:
        return None

    decoder = json.JSONDecoder()
    findings: list[dict] = []
    position = findings_array_match.end()

    while position < len(text):
        while position < len(text) and text[position] in " \t\n\r,":
            position += 1
        if position >= len(text) or text[position] in ("]", "}"):
            break
        if text[position] != "{":
            break
        try:
            item, end_pos = decoder.raw_decode(text, position)
            findings.append(item)
            position = end_pos
        except json.JSONDecodeError:
            break

    if not findings and summary == "Review completed (output truncated).":
        return None

    return {"summary": summary, "findings": findings, "critical_count": 0}


def build_review_summary(data: dict) -> ReviewSummary:
    comments: list[ReviewComment] = []
    for finding in data.get("findings", []):
        raw_severity = str(finding.get("severity", Severity.MEDIUM.value)).lower()
        try:
            severity = Severity(raw_severity)
        except ValueError:
            severity = Severity.MEDIUM
        comments.append(
            ReviewComment(
                file=finding["file"],
                line=finding["line"],
                end_line=finding.get("end_line"),
                severity=severity,
                body=finding["message"],
                category=str(finding.get("category", "") or "").lower() or None,
                confidence=str(finding.get("confidence", "") or "").lower() or None,
                fixable=finding.get("fixable") if isinstance(finding.get("fixable"), bool) else None,
                patch_group_key=str(finding.get("patch_group_key", "") or "") or None,
                suggestion=finding.get("suggestion"),
                old_code=finding.get("old_code"),
            )
        )
    critical_count = int(data.get("critical_count", 0) or 0)
    return ReviewSummary(
        summary=data.get("summary", "Review completed."),
        findings=comments,
        critical_count=critical_count,
        has_critical=critical_count > 0,
    )


def parse_review_response(
    text: str,
    *,
    debug: bool,
    logger: logging.Logger,
    fallback_review: Callable[[], ReviewSummary],
) -> tuple[ReviewSummary, str]:
    """Parse Gemini JSON response into a ReviewSummary."""
    try:
        sanitized = sanitize_json(text)
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(sanitized)
        return build_review_summary(data), "full"
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        if debug:
            logger.warning(f"Failed to parse Gemini response: {error}\nRaw: {text[:500]}")
        else:
            logger.debug("Failed to parse Gemini response; attempting truncation recovery")

    recovered = recover_truncated_json(text)
    if recovered:
        if debug:
            logger.info(
                f"Recovered {len(recovered['findings'])} finding(s) from truncated Gemini response"
            )
        return build_review_summary(recovered), "recovered"

    logger.error("Gemini response unrecoverable — returning empty review")
    return fallback_review(), "failed"


def parse_fix_response(
    text: str,
    default_findings: list[ReviewComment],
) -> list[tuple[list[int], ReviewComment]]:
    """Parse grouped patch response into indexed edits."""
    try:
        sanitized = sanitize_json(text)
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
