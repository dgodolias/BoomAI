"""Gemini AI review integration — same httpx pattern as DataViz."""

import json
import logging

import httpx

from scripts.config import settings
from scripts.models import Finding, ReviewComment, ReviewSummary, Severity
from scripts.prompts import SYSTEM_PROMPT, REVIEW_USER_TEMPLATE

logger = logging.getLogger(__name__)


async def review_with_gemini(
    diff: str,
    findings: list[Finding],
    changed_files: list[dict],
) -> ReviewSummary:
    """
    Send PR diff + static findings to Gemini for AI review.

    URL pattern: {base_url}/{model}:generateContent?key={api_key}
    Same as DataViz llm_service.py.
    """
    findings_json = json.dumps(
        [f.model_dump() for f in findings],
        indent=2,
        ensure_ascii=False,
    )

    user_message = REVIEW_USER_TEMPLATE.format(
        diff=_truncate_diff(diff),
        finding_count=len(findings),
        findings_json=findings_json,
    )

    url = (
        f"{settings.gemini_base_url}/{settings.llm_model}"
        f":generateContent?key={settings.google_api_key}"
    )

    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
            response = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json={
                    "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                    "contents": [
                        {"role": "user", "parts": [{"text": user_message}]}
                    ],
                    "generationConfig": {
                        "maxOutputTokens": settings.max_output_tokens,
                        "temperature": 0.1,
                        "responseMimeType": "application/json",
                    },
                },
            )

            if response.status_code != 200:
                logger.error(
                    f"Gemini API error {response.status_code}: {response.text[:500]}"
                )
                return _fallback_review(findings)

            result = response.json()

            # Error check (DataViz pattern)
            if "error" in result:
                error_msg = result["error"].get("message", "Unknown error")
                logger.error(f"Gemini API error: {error_msg}")
                return _fallback_review(findings)

            # Validate response structure
            if "candidates" not in result or not result["candidates"]:
                logger.error(
                    f"Malformed Gemini response: {json.dumps(result)[:500]}"
                )
                return _fallback_review(findings)

            candidate = result["candidates"][0]
            if "content" not in candidate or "parts" not in candidate["content"]:
                logger.error(
                    f"Malformed candidate: {json.dumps(candidate)[:500]}"
                )
                return _fallback_review(findings)

            # Extract text (DataViz pattern)
            text = candidate["content"]["parts"][0]["text"]
            return _parse_review_response(text, findings)

    except httpx.TimeoutException:
        logger.error("Gemini API timed out")
        return _fallback_review(findings)
    except Exception as e:
        logger.exception(f"Gemini review failed: {e}")
        return _fallback_review(findings)


def _truncate_diff(diff: str) -> str:
    """Truncate diff if it exceeds the max char limit."""
    if len(diff) <= settings.max_diff_chars:
        return diff
    logger.warning(
        f"Diff too large ({len(diff)} chars), truncating to {settings.max_diff_chars}"
    )
    return (
        diff[: settings.max_diff_chars]
        + "\n\n... [DIFF TRUNCATED - showing first portion only]"
    )


def _parse_review_response(
    text: str, original_findings: list[Finding]
) -> ReviewSummary:
    """Parse Gemini's JSON response into a ReviewSummary."""
    try:
        data = json.loads(text)
        comments = []
        for f in data.get("findings", []):
            comments.append(
                ReviewComment(
                    file=f["file"],
                    line=f["line"],
                    end_line=f.get("end_line"),
                    body=f["message"],
                    suggestion=f.get("suggestion"),
                )
            )

        critical_count = data.get("critical_count", 0)
        return ReviewSummary(
            summary=data.get("summary", "Review completed."),
            findings=comments,
            critical_count=critical_count,
            has_critical=critical_count > 0,
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.error(f"Failed to parse Gemini response: {e}\nRaw: {text[:500]}")
        return _fallback_review(original_findings)


def _fallback_review(findings: list[Finding]) -> ReviewSummary:
    """If Gemini fails, create review from static findings only."""
    comments = [
        ReviewComment(
            file=f.file,
            line=f.line,
            end_line=f.end_line,
            body=f"**[{f.source.value}] {f.rule_id}**\n\n{f.message}",
            suggestion=f.suggestion,
        )
        for f in findings
    ]
    critical = sum(
        1 for f in findings if f.severity in (Severity.CRITICAL, Severity.HIGH)
    )
    return ReviewSummary(
        summary="AI review unavailable. Showing static analysis findings only.",
        findings=comments,
        critical_count=critical,
        has_critical=critical > 0,
    )
