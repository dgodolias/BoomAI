"""Gemini AI review integration — same httpx pattern as DataViz."""

import json
import logging

import httpx

from boomai.config import settings
from boomai.models import Finding, ReviewComment, ReviewSummary, Severity
from boomai.prompts import (
    build_system_prompt, build_user_message,
    build_scan_system_prompt, build_scan_user_message,
)

logger = logging.getLogger(__name__)


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

    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
            response = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json={
                    "system_instruction": {"parts": [{"text": system_prompt}]},
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
    """Fix common Gemini JSON issues: trailing commas."""
    import re
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


def _parse_review_response(
    text: str, original_findings: list[Finding]
) -> ReviewSummary:
    """Parse Gemini's JSON response into a ReviewSummary."""
    try:
        sanitized = _sanitize_json(text)
        # Use raw_decode to handle extra data after the JSON object
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


# ============================================================
#  Full-codebase scan
# ============================================================

def _chunk_files(
    file_contents: list[tuple[str, str]],
    char_budget: int,
) -> list[list[tuple[str, str]]]:
    """Split files into chunks that fit within the character budget."""
    chunks: list[list[tuple[str, str]]] = []
    current_chunk: list[tuple[str, str]] = []
    current_size = 0

    # Sort smallest first to maximise files per chunk
    sorted_files = sorted(file_contents, key=lambda x: len(x[1]))

    for path, content in sorted_files:
        file_size = len(content) + len(path) + 20  # header overhead
        if current_size + file_size > char_budget and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0
        current_chunk.append((path, content))
        current_size += file_size

    if current_chunk:
        chunks.append(current_chunk)

    return chunks if chunks else [[]]


async def _scan_chunk(
    file_contents: list[tuple[str, str]],
    findings: list[Finding],
    detected_languages: list[str],
    chunk_info: str = "",
) -> ReviewSummary:
    """Send a single chunk of files to Gemini for scan review."""
    system_prompt = build_scan_system_prompt(detected_languages)

    # Filter findings to only files in this chunk
    chunk_files = {path for path, _ in file_contents}
    chunk_findings = [f for f in findings if f.file in chunk_files]

    findings_json = json.dumps(
        [f.model_dump() for f in chunk_findings],
        indent=2,
        ensure_ascii=False,
    )

    user_message = build_scan_user_message(
        file_contents=file_contents,
        finding_count=len(chunk_findings),
        findings_json=findings_json,
        detected_languages=detected_languages,
        chunk_info=chunk_info,
    )

    url = (
        f"{settings.gemini_base_url}/{settings.llm_model}"
        f":generateContent?key={settings.google_api_key}"
    )

    try:
        async with httpx.AsyncClient(timeout=settings.scan_timeout) as client:
            response = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json={
                    "system_instruction": {"parts": [{"text": system_prompt}]},
                    "contents": [
                        {"role": "user", "parts": [{"text": user_message}]}
                    ],
                    "generationConfig": {
                        "maxOutputTokens": settings.scan_output_tokens,
                        "temperature": 0.1,
                        "responseMimeType": "application/json",
                    },
                },
            )

            if response.status_code != 200:
                logger.error(
                    f"Gemini API error {response.status_code}: {response.text[:500]}"
                )
                return _fallback_review(chunk_findings)

            result = response.json()

            if "error" in result:
                logger.error(f"Gemini API error: {result['error'].get('message', 'Unknown')}")
                return _fallback_review(chunk_findings)

            if "candidates" not in result or not result["candidates"]:
                logger.error(f"Malformed Gemini response: {json.dumps(result)[:500]}")
                return _fallback_review(chunk_findings)

            candidate = result["candidates"][0]
            if "content" not in candidate or "parts" not in candidate["content"]:
                logger.error(f"Malformed candidate: {json.dumps(candidate)[:500]}")
                return _fallback_review(chunk_findings)

            text = candidate["content"]["parts"][0]["text"]
            return _parse_review_response(text, chunk_findings)

    except httpx.TimeoutException:
        logger.error("Gemini API timed out")
        return _fallback_review(chunk_findings)
    except Exception as e:
        logger.exception(f"Gemini scan failed: {e}")
        return _fallback_review(chunk_findings)


async def scan_with_gemini(
    file_contents: list[tuple[str, str]],
    findings: list[Finding],
    detected_languages: list[str] | None = None,
) -> ReviewSummary:
    """Send full file contents + static findings to Gemini for codebase scan."""
    if detected_languages is None:
        detected_languages = []

    chunks = _chunk_files(file_contents, settings.max_scan_chars)

    if len(chunks) == 1:
        return await _scan_chunk(chunks[0], findings, detected_languages)

    # Multiple chunks — call Gemini for each, merge results
    all_findings: list[ReviewComment] = []
    summaries: list[str] = []
    total_critical = 0

    for i, chunk in enumerate(chunks, 1):
        chunk_info = f"Chunk {i} of {len(chunks)}"
        logger.info(f"Scanning {chunk_info} ({len(chunk)} files)")
        result = await _scan_chunk(chunk, findings, detected_languages, chunk_info)
        all_findings.extend(result.findings)
        summaries.append(result.summary)
        total_critical += result.critical_count

    combined_summary = " | ".join(summaries)
    return ReviewSummary(
        summary=combined_summary,
        findings=all_findings,
        critical_count=total_critical,
        has_critical=total_critical > 0,
    )


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
