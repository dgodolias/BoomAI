"""Gemini AI review integration — same httpx pattern as DataViz."""

import asyncio
import json
import logging

import httpx

from scripts.config import settings
from scripts.models import Finding, ReviewComment, ReviewSummary, Severity
from scripts.prompts import build_system_prompt, build_user_message

logger = logging.getLogger(__name__)


async def _gemini_post(url: str, payload: dict, timeout: float,
                       max_retries: int = 3) -> httpx.Response | None:
    """POST to Gemini API with retry on transient network errors."""
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                )
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(f"Gemini API attempt {attempt + 1} failed ({e}), retrying in {wait}s...")
                await asyncio.sleep(wait)
            else:
                logger.error(f"Gemini API failed after {max_retries} attempts: {e}")
                return None
    return None


async def review_with_gemini(
    diff: str,
    findings: list[Finding],
    changed_files: list[dict],
    detected_languages: list[str] | None = None,
) -> ReviewSummary:
    """
    Send PR diff + static findings to Gemini for AI review.

    URL pattern: {base_url}/{model}:generateContent?key={api_key}
    Same as DataViz llm_service.py.
    """
    if detected_languages is None:
        detected_languages = []

    system_prompt = build_system_prompt(detected_languages)

    findings_json = json.dumps(
        [f.model_dump() for f in findings],
        indent=2,
        ensure_ascii=False,
    )

    user_message = build_user_message(
        diff=_truncate_diff(diff),
        finding_count=len(findings),
        findings_json=findings_json,
        detected_languages=detected_languages,
    )

    url = (
        f"{settings.gemini_base_url}/{settings.llm_model}"
        f":generateContent?key={settings.google_api_key}"
    )

    response = await _gemini_post(url, {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": {
            "maxOutputTokens": settings.max_output_tokens,
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }, timeout=settings.llm_timeout)

    if response is None:
        return _fallback_review(findings)

    if response.status_code != 200:
        logger.error(f"Gemini API error {response.status_code}: {response.text[:500]}")
        return _fallback_review(findings)

    result = response.json()

    if "error" in result:
        logger.error(f"Gemini API error: {result['error'].get('message', 'Unknown')}")
        return _fallback_review(findings)

    if "candidates" not in result or not result["candidates"]:
        logger.error(f"Malformed Gemini response: {json.dumps(result)[:500]}")
        return _fallback_review(findings)

    candidate = result["candidates"][0]
    if "content" not in candidate or "parts" not in candidate["content"]:
        logger.error(f"Malformed candidate: {json.dumps(candidate)[:500]}")
        return _fallback_review(findings)

    text = candidate["content"]["parts"][0]["text"]
    return _parse_review_response(text, findings)


def _truncate_diff(diff: str) -> str:
    """Truncate diff at file boundaries if it exceeds the max char limit."""
    if len(diff) <= settings.max_diff_chars:
        return diff

    logger.warning(
        f"Diff too large ({len(diff)} chars), truncating to {settings.max_diff_chars}"
    )

    # Split into per-file diffs and keep whole files until budget runs out
    file_diffs = _split_diff_by_file(diff)
    kept = []
    total = 0
    skipped = []
    for filename, file_diff in file_diffs:
        if total + len(file_diff) > settings.max_diff_chars and kept:
            skipped.append(filename)
            continue
        kept.append(file_diff)
        total += len(file_diff)

    result = "".join(kept)
    if skipped:
        result += (
            f"\n\n... [DIFF TRUNCATED — {len(skipped)} file(s) omitted: "
            f"{', '.join(skipped[:10])}"
            f"{'...' if len(skipped) > 10 else ''}]"
        )
    return result


def _split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified diff into (filename, diff_chunk) pairs."""
    import re
    parts = re.split(r'(?=^diff --git )', diff, flags=re.MULTILINE)
    result = []
    for part in parts:
        if not part.strip():
            continue
        match = re.search(r'^diff --git a/(.+?) b/', part, re.MULTILINE)
        filename = match.group(1) if match else "unknown"
        result.append((filename, part))
    return result


def _sanitize_json(text: str) -> str:
    """Fix common Gemini JSON issues: trailing commas, truncated output."""
    import re
    # Remove trailing commas before } or ]
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


def _parse_review_response(
    text: str, original_findings: list[Finding]
) -> ReviewSummary:
    """Parse Gemini's JSON response into a ReviewSummary."""
    try:
        sanitized = _sanitize_json(text)
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(sanitized)
        comments = []
        for f in data.get("findings", []):
            comments.append(
                ReviewComment(
                    file=f["file"],
                    line=f["line"],
                    end_line=f.get("end_line"),
                    body=f["message"],
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
