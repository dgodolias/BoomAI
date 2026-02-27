"""Gemini AI review integration — same httpx pattern as DataViz."""

import asyncio
import json
import logging
import time
from typing import Callable

import httpx

from .config import settings
from .models import ReviewComment, ReviewSummary
from .prompts import (
    build_scan_system_prompt, build_scan_user_message,
    build_plan_prompt, build_plan_user_message,
)

logger = logging.getLogger(__name__)

# Type alias for progress callback
ProgressFn = Callable[[str], None] | None


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
        except (httpx.ConnectError, httpx.TimeoutException) as e:
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
    import re
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


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


def _parse_review_response(text: str) -> ReviewSummary:
    """Parse Gemini's JSON response into a ReviewSummary."""
    def _build_summary(data: dict) -> ReviewSummary:
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

    # --- primary parse ---
    try:
        sanitized = _sanitize_json(text)
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(sanitized)
        return _build_summary(data)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"Failed to parse Gemini response: {e}\nRaw: {text[:500]}")

    # --- truncation recovery ---
    recovered = _recover_truncated_json(text)
    if recovered:
        logger.info(
            f"Recovered {len(recovered['findings'])} finding(s) from truncated Gemini response"
        )
        return _build_summary(recovered)

    logger.error("Gemini response unrecoverable — returning empty review")
    return _fallback_review()


# ============================================================
#  Full-codebase scan
# ============================================================

def _build_repo_map(file_contents: list[tuple[str, str]]) -> str:
    """Build a tree-view string of the repo for LLM planning.

    Output looks like:
      Assets/Scripts/Behaviours/Ped/
        Ped.cs                          (850 lines, 28.1K)
        PedModel.cs                     (120 lines, 3.9K)
      Assets/Scripts/Networking/
        PedSync.cs                      (180 lines, 5.8K)
    """
    from collections import defaultdict
    import posixpath

    # Group files by directory
    dirs: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for path, content in file_contents:
        norm = path.replace("\\", "/")
        folder = posixpath.dirname(norm) or "."
        lines = content.count("\n") + 1
        chars = len(content)
        dirs[folder].append((posixpath.basename(norm), lines, chars))

    lines_out: list[str] = []
    for folder in sorted(dirs):
        files = sorted(dirs[folder], key=lambda x: -x[2])  # largest first
        dir_chars = sum(c for _, _, c in files)
        lines_out.append(f"  {folder}/  ({len(files)} files, {dir_chars:,} chars)")
        for name, line_count, char_count in files:
            size_str = (
                f"{char_count / 1000:.1f}K" if char_count >= 1000
                else f"{char_count}"
            )
            lines_out.append(f"    {name:<45} ({line_count} lines, {size_str})")

    return "\n".join(lines_out)


async def _plan_chunks(
    file_contents: list[tuple[str, str]],
    detected_languages: list[str],
    char_budget: int,
    on_progress: ProgressFn = None,
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

    url = (
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
                    "generationConfig": {
                        "maxOutputTokens": settings.plan_output_tokens,
                        "temperature": 0.1,
                        "responseMimeType": "application/json",
                    },
                }, timeout=settings.plan_timeout, on_retry=_on_retry),
                timeout=settings.plan_timeout,
            ),
            emit=on_progress, label="Planning",
        )
    except asyncio.TimeoutError:
        _emit(f"  Planning hard timeout at {int(settings.plan_timeout)}s, using greedy chunking")
        return None

    if response is None or response.status_code != 200:
        status = f" (HTTP {response.status_code})" if response else ""
        _emit(f"  Planning failed{status}, using greedy chunking")
        return None

    try:
        result = response.json()
        text = result["candidates"][0]["content"]["parts"][0]["text"]
        sanitized = _sanitize_json(text)
        data = json.loads(sanitized)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logger.warning(f"Failed to parse planning response: {e}")
        return None

    # Build lookup: normalized path -> (path, content)
    content_map: dict[str, tuple[str, str]] = {}
    for path, content in file_contents:
        content_map[path.replace("\\", "/")] = (path, content)

    # Validate and build chunks
    planned_chunks: list[list[tuple[str, str]]] = []
    seen: set[str] = set()

    for chunk_def in data.get("chunks", []):
        chunk_files: list[tuple[str, str]] = []
        chunk_chars = 0
        for file_path in chunk_def.get("files", []):
            norm = file_path.replace("\\", "/")
            if norm not in content_map:
                logger.warning(f"Planning: unknown file '{file_path}', skipping")
                continue
            if norm in seen:
                logger.warning(f"Planning: duplicate file '{file_path}', skipping")
                continue
            seen.add(norm)
            pair = content_map[norm]
            chunk_files.append(pair)
            chunk_chars += len(pair[1])
        if chunk_files:
            planned_chunks.append(chunk_files)

    # Check coverage — every file must be assigned
    all_paths = set(p.replace("\\", "/") for p, _ in file_contents)
    missing = all_paths - seen
    if missing:
        logger.warning(
            f"Planning missed {len(missing)} file(s), appending as extra chunk"
        )
        extra = [content_map[p] for p in sorted(missing)]
        planned_chunks.append(extra)

    if not planned_chunks:
        _emit("  Planning returned empty chunks, using greedy chunking")
        return None

    focus_list = [c.get("focus", "") for c in data.get("chunks", [])]
    for i, ch in enumerate(planned_chunks):
        focus = f" — {focus_list[i]}" if i < len(focus_list) and focus_list[i] else ""
        chars = sum(len(c) for _, c in ch)
        _emit(f"  Chunk {i+1}: {len(ch)} files, {chars:,} chars{focus}")
    return planned_chunks


def _chunk_files(
    file_contents: list[tuple[str, str]],
    char_budget: int,
) -> list[list[tuple[str, str]]]:
    """Split files into chunks that fit within the character budget."""
    chunks: list[list[tuple[str, str]]] = []
    current_chunk: list[tuple[str, str]] = []
    current_size = 0

    # Sort largest first — complex files get maximum LLM attention
    sorted_files = sorted(file_contents, key=lambda x: len(x[1]), reverse=True)

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
    detected_languages: list[str],
    chunk_info: str = "",
    comments: bool = False,
    explanations: bool = True,
    on_progress: ProgressFn = None,
) -> ReviewSummary:
    """Send a single chunk of files to Gemini for scan review."""
    system_prompt = build_scan_system_prompt(
        detected_languages, comments=comments, explanations=explanations,
    )

    user_message = build_scan_user_message(
        file_contents=file_contents,
        detected_languages=detected_languages,
        chunk_info=chunk_info,
    )

    url = (
        f"{settings.gemini_base_url}/{settings.llm_model}"
        f":generateContent?key={settings.google_api_key}"
    )

    def _on_retry(attempt: int, msg: str) -> None:
        if on_progress:
            on_progress(f"  {chunk_info} — attempt {attempt} failed: {msg}")

    try:
        response = await _with_heartbeat(
            asyncio.wait_for(
                _gemini_post(url, {
                    "system_instruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_message}]}],
                    "generationConfig": {
                        "maxOutputTokens": settings.scan_output_tokens,
                        "temperature": 0.1,
                        "responseMimeType": "application/json",
                    },
                }, timeout=settings.scan_timeout, on_retry=_on_retry),
                timeout=settings.scan_timeout,
            ),
            emit=on_progress, label=chunk_info or "Reviewing",
        )
    except asyncio.TimeoutError:
        if on_progress:
            on_progress(f"  {chunk_info} — hard timeout at {int(settings.scan_timeout)}s")
        return _fallback_review()

    if response is None:
        return _fallback_review()

    if response.status_code != 200:
        logger.error(f"Gemini API error {response.status_code}: {response.text[:500]}")
        return _fallback_review()

    result = response.json()

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

    text = candidate["content"]["parts"][0]["text"]
    return _parse_review_response(text)


async def scan_with_gemini(
    file_contents: list[tuple[str, str]],
    detected_languages: list[str] | None = None,
    comments: bool = False,
    explanations: bool = True,
    on_progress: Callable[[str], None] | None = None,
    on_chunk_done: Callable[[ReviewSummary], None] | None = None,
) -> ReviewSummary:
    """Send full file contents + static findings to Gemini for codebase scan.

    on_chunk_done is called with each chunk's ReviewSummary as soon as it
    completes, enabling incremental apply (chunks have disjoint file sets).
    """
    if detected_languages is None:
        detected_languages = []

    def _emit(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # Try LLM-planned chunks first, fall back to greedy
    # Skip LLM planning for large repos — greedy is fast and reliable
    if len(file_contents) > 300:
        _emit(f"Large repo ({len(file_contents)} files) — using greedy chunking")
        chunks = _chunk_files(file_contents, settings.max_scan_chars)
    else:
        _emit("Planning review chunks...")
        chunks = await _plan_chunks(
            file_contents, detected_languages, settings.max_scan_chars,
            on_progress=on_progress,
        )
        if chunks is None:
            chunks = _chunk_files(file_contents, settings.max_scan_chars)
    _emit(f"{len(chunks)} chunk(s) planned")

    if len(chunks) == 1:
        chars = sum(len(c) for _, c in chunks[0])
        _emit(f"Reviewing 1 chunk ({len(chunks[0])} files, {chars:,} chars)...")

        # Use same split-on-fail logic as multi-chunk path
        async def _review_single(
            chunk: list[tuple[str, str]], label: str,
        ) -> ReviewSummary:
            result = await _scan_chunk(
                chunk, detected_languages, label,
                comments=comments, explanations=explanations,
                on_progress=on_progress,
            )
            if result.summary == "AI review unavailable." and len(chunk) > 5:
                mid = len(chunk) // 2
                _emit(f"{label} failed — splitting and retrying")
                a = await _review_single(chunk[:mid], f"{label}a")
                b = await _review_single(chunk[mid:], f"{label}b")
                merged = list(a.findings) + list(b.findings)
                crit = a.critical_count + b.critical_count
                return ReviewSummary(
                    summary=f"{a.summary} | {b.summary}",
                    findings=merged, critical_count=crit, has_critical=crit > 0,
                )
            return result

        result = await _review_single(chunks[0], "Chunk 1/1")
        _emit(f"Done — {len(result.findings)} issues found")
        if on_chunk_done:
            on_chunk_done(result)
        return result

    # Multiple chunks — call Gemini concurrently, merge results
    max_concurrent = 3
    sem = asyncio.Semaphore(max_concurrent)
    completed = 0
    _emit(f"Reviewing code [{len(chunks)} chunks, {max_concurrent} concurrent]")

    async def _review_chunk(
        chunk: list[tuple[str, str]], label: str,
    ) -> ReviewSummary:
        """Scan a chunk; on failure, split in half and retry sub-chunks."""
        chars = sum(len(c) for _, c in chunk)
        _emit(f"{label} {len(chunk)} files, {chars:,} chars...")
        result = await _scan_chunk(
            chunk, detected_languages, label,
            comments=comments, explanations=explanations,
            on_progress=on_progress,
        )
        # If failed and chunk is splittable, split in half and retry
        if result.summary == "AI review unavailable." and len(chunk) > 5:
            mid = len(chunk) // 2
            _emit(f"{label} failed — splitting into 2 sub-chunks and retrying")
            sub_a = await _review_chunk(chunk[:mid], f"{label}a")
            sub_b = await _review_chunk(chunk[mid:], f"{label}b")
            merged_findings = list(sub_a.findings) + list(sub_b.findings)
            merged_crit = sub_a.critical_count + sub_b.critical_count
            return ReviewSummary(
                summary=f"{sub_a.summary} | {sub_b.summary}",
                findings=merged_findings,
                critical_count=merged_crit,
                has_critical=merged_crit > 0,
            )
        return result

    async def _scan_with_sem(
        chunk: list[tuple[str, str]], idx: int,
    ) -> ReviewSummary:
        nonlocal completed
        async with sem:
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

    combined_summary = " | ".join(summaries)
    return ReviewSummary(
        summary=combined_summary,
        findings=all_findings,
        critical_count=total_critical,
        has_critical=total_critical > 0,
    )


def _fallback_review() -> ReviewSummary:
    """Return an empty review when Gemini fails."""
    return ReviewSummary(
        summary="AI review unavailable.",
        findings=[],
        critical_count=0,
        has_critical=False,
    )
