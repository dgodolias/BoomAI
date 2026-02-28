"""Gemini AI review integration — same httpx pattern as DataViz."""

import asyncio
import json
import logging
import time
from typing import Callable

import httpx

from .config import settings
from .models import ReviewComment, ReviewSummary, UsageStats
from .prompts import (
    build_scan_system_prompt, build_scan_user_message,
    build_plan_prompt, build_plan_user_message,
)

logger = logging.getLogger(__name__)

# Type alias for progress callback
ProgressFn = Callable[[str], None] | None

# Model fallback chain — each model has separate rate limits (RPM/RPD)
_FALLBACK_MODELS = [
    "gemini-3.1-pro-preview-customtools",
    "gemini-2.5-pro",
    "gemini-3-flash",
    "gemini-2.5-flash",
]


class _ModelChain:
    """Thread-safe model fallback chain for 429 rate limits."""

    def __init__(self, initial_model: str):
        self._models = list(_FALLBACK_MODELS)
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
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as e:
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
            usage.add(result)
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
        if norm.startswith("./"):
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
    for i, ch in enumerate(planned_chunks):
        chars = sum(len(c) for _, c in ch)
        focus = f" — {focus_list[i]}" if i < len(focus_list) and focus_list[i] else ""
        if chars > char_budget:
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
    model_chain: _ModelChain | None = None,
    usage: UsageStats | None = None,
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

    url = model_chain.build_url() if model_chain else (
        f"{settings.gemini_base_url}/{settings.llm_model}"
        f":generateContent?key={settings.google_api_key}"
    )

    def _on_retry(attempt: int, msg: str) -> None:
        if on_progress:
            on_progress(f"  {chunk_info} — attempt {attempt} failed: {msg}")

    # Give single-file chunks 2x timeout — they can't be split further
    effective_timeout = settings.scan_timeout
    if len(file_contents) == 1:
        effective_timeout = settings.scan_timeout * 2

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
                    comments=comments, explanations=explanations,
                    on_progress=on_progress, model_chain=model_chain,
                    usage=usage,
                )
        logger.error("Rate limited (429) — no fallback models left")
        return _rate_limited_review()

    if response.status_code != 200:
        logger.error(f"Gemini API error {response.status_code}: {response.text[:500]}")
        return _fallback_review()

    result = response.json()
    if usage:
        usage.add(result)

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

    model_chain = _ModelChain(settings.llm_model)
    usage = UsageStats()

    # Always try LLM planning first — hard timeout protects us
    _emit("Planning review chunks...")
    chunks = await _plan_chunks(
        file_contents, detected_languages, settings.max_scan_chars,
        on_progress=on_progress, model_chain=model_chain, usage=usage,
    )
    if chunks is None:
        _emit("Using greedy chunking")
        chunks = _chunk_files(file_contents, settings.max_scan_chars)
    _emit(f"{len(chunks)} chunk(s) planned, model: {model_chain.current}")

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
                on_progress=on_progress, model_chain=model_chain,
                usage=usage,
            )
            # 429 — stop immediately, don't split
            if result.summary == "Rate limited.":
                _emit("Rate limited — aborting")
                return result
            if result.summary == "AI review unavailable." and len(chunk) > 1:
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
        result.usage = usage
        _emit(f"Done — {len(result.findings)} issues found")
        if on_chunk_done:
            on_chunk_done(result)
        return result

    # Multiple chunks — call Gemini concurrently, merge results
    max_concurrent = 3 if len(chunks) <= 20 else 2
    sem = asyncio.Semaphore(max_concurrent)
    stop_event = asyncio.Event()  # set on 429 — stops all remaining chunks
    completed = 0
    _emit(f"Reviewing code [{len(chunks)} chunks, {max_concurrent} concurrent]")

    async def _review_chunk(
        chunk: list[tuple[str, str]], label: str,
    ) -> ReviewSummary:
        """Scan a chunk; on failure, split in half and retry sub-chunks."""
        if stop_event.is_set():
            return _rate_limited_review()
        chars = sum(len(c) for _, c in chunk)
        _emit(f"{label} {len(chunk)} files, {chars:,} chars...")
        result = await _scan_chunk(
            chunk, detected_languages, label,
            comments=comments, explanations=explanations,
            on_progress=on_progress, model_chain=model_chain,
            usage=usage,
        )
        # 429 with all models exhausted — stop everything, don't split
        if result.summary == "Rate limited.":
            if not stop_event.is_set():
                _emit("All models rate limited — stopping remaining chunks")
                stop_event.set()
            return result
        # If failed and chunk is splittable, split in half and retry
        if result.summary == "AI review unavailable." and len(chunk) > 1:
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

    combined_summary = " | ".join(summaries)
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


def _rate_limited_review() -> ReviewSummary:
    """Return an empty review for 429 rate limit — must NOT trigger auto-split."""
    return ReviewSummary(
        summary="Rate limited.",
        findings=[],
        critical_count=0,
        has_critical=False,
    )
