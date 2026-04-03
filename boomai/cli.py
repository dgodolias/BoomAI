"""
BoomAI CLI — AI-powered code fixer

Usage (from inside your project):
  boomai fix                      # scan + auto-fix codebase
  boomai settings                 # configure API key & preferences
"""

import argparse
import asyncio
import difflib
import logging
import os
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from boomai.analysis.languages import detect_languages, filter_reviewable_files
from boomai.app.orchestrator import run_static_analysis_suite
from boomai.context.indexer import build_code_index
from boomai.core.config import settings
from boomai.core.models import ReviewSummary

logger = logging.getLogger(__name__)


# ============================================================
#  Banner
# ============================================================

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_BOOM_COLORS = [
    (104, 41, 196),   # boom purple
    (139, 48, 214),   # bright violet
    (180, 60, 221),   # neon violet
    (222, 82, 202),   # magenta
    (255, 119, 171),  # boom pink
]


def _enable_windows_vt() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        if handle in (0, -1):
            return False
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return False
        if kernel32.SetConsoleMode(handle, mode.value | 0x0004) == 0:
            return False
        return True
    except Exception:
        return False


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    term = os.environ.get("TERM", "")
    if term.lower() == "dumb":
        return False
    return _enable_windows_vt()


def _rgb(color: tuple[int, int, int], text: str, *, bold: bool = False, dim: bool = False) -> str:
    prefix = []
    if bold:
        prefix.append(_BOLD)
    if dim:
        prefix.append(_DIM)
    prefix.append(f"\033[38;2;{color[0]};{color[1]};{color[2]}m")
    return "".join(prefix) + text + _RESET


def _gradient_text(text: str, colors: list[tuple[int, int, int]]) -> str:
    if not text:
        return text
    if not _supports_color():
        return text

    colored: list[str] = []
    steps = max(1, len(text) - 1)
    segments = len(colors) - 1

    for index, char in enumerate(text):
        if char == " ":
            colored.append(char)
            continue
        position = index / steps
        segment = min(segments - 1, int(position * segments))
        local_start = segment / segments
        local_end = (segment + 1) / segments
        local_t = 0.0 if local_end == local_start else (position - local_start) / (local_end - local_start)
        start = colors[segment]
        end = colors[segment + 1]
        color = (
            int(start[0] + (end[0] - start[0]) * local_t),
            int(start[1] + (end[1] - start[1]) * local_t),
            int(start[2] + (end[2] - start[2]) * local_t),
        )
        colored.append(_rgb(color, char, bold=True))
    return "".join(colored)


def _gradient_block_line(text: str, colors: list[tuple[int, int, int]]) -> str:
    if not text or not _supports_color():
        return text

    visible_columns = [index for index, char in enumerate(text) if char != " "]
    if not visible_columns:
        return text

    start_col = visible_columns[0]
    end_col = visible_columns[-1]
    span = max(1, end_col - start_col)
    segments = len(colors) - 1
    colored: list[str] = []

    for index, char in enumerate(text):
        if char == " ":
            colored.append(char)
            continue
        position = (index - start_col) / span
        segment = min(segments - 1, int(position * segments))
        local_start = segment / segments
        local_end = (segment + 1) / segments
        local_t = 0.0 if local_end == local_start else (position - local_start) / (local_end - local_start)
        start = colors[segment]
        end = colors[segment + 1]
        color = (
            int(start[0] + (end[0] - start[0]) * local_t),
            int(start[1] + (end[1] - start[1]) * local_t),
            int(start[2] + (end[2] - start[2]) * local_t),
        )
        colored.append(_rgb(color, char, bold=True))
    return "".join(colored)


def _render_pixel_banner_line(pattern: str, colors: list[tuple[int, int, int]]) -> str:
    if not pattern or not _supports_color():
        return pattern

    visible_columns = [index for index, char in enumerate(pattern) if char != " "]
    if not visible_columns:
        return pattern

    start_col = visible_columns[0]
    end_col = visible_columns[-1]
    span = max(1, end_col - start_col)
    segments = len(colors) - 1
    rendered: list[str] = []

    for index, char in enumerate(pattern):
        if char == " ":
            rendered.append(char)
            continue

        position = (index - start_col) / span
        segment = min(segments - 1, int(position * segments))
        local_start = segment / segments
        local_end = (segment + 1) / segments
        local_t = 0.0 if local_end == local_start else (position - local_start) / (local_end - local_start)
        start = colors[segment]
        end = colors[segment + 1]
        color = (
            int(start[0] + (end[0] - start[0]) * local_t),
            int(start[1] + (end[1] - start[1]) * local_t),
            int(start[2] + (end[2] - start[2]) * local_t),
        )

        if char == "░":
            rendered.append(_rgb(color, char, dim=True))
        else:
            rendered.append(_rgb(color, char, bold=True))

    return "".join(rendered)


def print_banner():
    """Print a branded BoomAI terminal banner."""
    print()
    if _supports_color():
        accent = "pixel-powered code fixer for C#/Unity"
        divider = _rgb((186, 106, 220), "· " * 28, dim=True)
        title_lines = [
            "██████╗  ██████╗  ██████╗ ███╗   ███╗     █████╗ ██╗",
            "██╔══██╗██╔═══██╗██╔═══██╗████╗ ████║    ██╔══██╗██║",
            "██████╔╝██║   ██║██║   ██║██╔████╔██║    ███████║██║",
            "██╔══██╗██║   ██║██║   ██║██║╚██╔╝██║    ██╔══██║██║",
            "██████╔╝╚██████╔╝╚██████╔╝██║ ╚═╝ ██║    ██║  ██║██║",
            "╚═════╝  ╚═════╝  ╚═════╝ ╚═╝     ╚═╝    ╚═╝  ╚═╝╚═╝",
        ]

        print(f"  {divider}")
        print()
        for line in title_lines:
            print(f"    {_render_pixel_banner_line(line, _BOOM_COLORS)}")
        print()
        print(f"    {_rgb((214, 126, 220), accent, dim=True)}")
        print(f"  {divider}")
        print()
        return
        accent = "pixel-powered code fixer for C#/Unity"
        divider = _rgb((118, 36, 168), "· " * 28, dim=True)
        shadow_color = (84, 28, 118)
        title_lines = [
            "██████   ██████   ██████  ███    ███          █████  ██ ",
            "██   ██ ██    ██ ██    ██ ████  ████         ██   ██ ██ ",
            "██████  ██    ██ ██    ██ ██ ████ ██   ███   ███████ ██ ",
            "██   ██ ██    ██ ██    ██ ██  ██  ██         ██   ██ ██ ",
            "██████   ██████   ██████  ██      ██         ██   ██ ██ ",
        ]

        print(f"  {divider}")
        print()
        for line in title_lines:
            print(f"    {_gradient_block_line(line, _BOOM_COLORS)}")
            print(f"    {_rgb(shadow_color, line.replace('█', '░'), dim=True)}")
        print()
        print(f"    {_rgb((204, 120, 214), accent, dim=True)}")
        print(f"  {divider}")
        print()
        return
        accent = "pixel-powered code fixer for C#/Unity"
        divider = _rgb((90, 26, 126), "· " * 28, dim=True)
        shadow_color = (92, 43, 128)
        title_lines = [
            "██████   ██████   ██████  ███    ███          █████  ██ ",
            "██   ██ ██    ██ ██    ██ ████  ████         ██   ██ ██ ",
            "██████  ██    ██ ██    ██ ██ ████ ██   ███   ███████ ██ ",
            "██   ██ ██    ██ ██    ██ ██  ██  ██         ██   ██ ██ ",
            "██████   ██████   ██████  ██      ██         ██   ██ ██ ",
        ]

        print(f"  {divider}")
        print()
        for index, line in enumerate(title_lines):
            prefix = _rgb((255, 112, 158), "💣 ", bold=True) if index == 2 else "   "
            print(f"  {prefix}{_gradient_block_line(line, _BOOM_COLORS)}")
            shadow = "  " + line.replace("█", "░")
            print(f"      {_rgb(shadow_color, shadow, dim=True)}")
        print()
        print(f"      {_rgb((176, 126, 220), accent, dim=True)}")
        print(f"  {divider}")
        print()
        return
        accent = "pixel-powered code fixer for C#/Unity"
        divider = _rgb((73, 20, 102), "· " * 18, dim=True)
        bomb = _rgb((255, 112, 158), "💣", bold=True)
        title = _gradient_text("BOOMAI", _BOOM_COLORS)
        print(f"  {divider}")
        print(f"  {bomb} {title}")
        print(f"  {_rgb((111, 32, 147), accent, dim=True)}")
        print(f"  {divider}")
    else:
        print("  BOOMAI")
        print("  pixel-powered code fixer for C#/Unity")
    print()


# ============================================================
#  Local file operations
# ============================================================

def _is_natural_language(suggestion: str) -> bool:
    """Detect if a suggestion is natural language instructions, not code."""
    s = suggestion.strip()
    instruction_starters = (
        "Remove ", "Delete ", "Replace ", "Update ", "Change ",
        "Rename ", "Move ", "Add ", "Ensure ", "Consider ",
        "Refactor ", "Fix ", "Implement ", "Use ", "Convert ",
    )
    if s.startswith(instruction_starters):
        return True
    code_chars = {'{', '}', '(', ')', ';', '=', '<', '>', '[', ']'}
    if not any(c in s for c in code_chars):
        return True
    return False


def _line_match(content: str, old_code: str, hint_line: int) -> tuple[int, int] | None:
    """Find old_code lines in content using stripped comparison.

    Returns (start_line_idx, line_count) or None.
    Picks the match closest to hint_line when multiple exist.
    Uses two passes: first trailing-whitespace-tolerant, then
    fully-stripped fallback for indentation mismatches.
    """
    content_lines = content.split('\n')
    old_lines = old_code.strip().split('\n')
    old_stripped = [l.rstrip() for l in old_lines]

    # Remove empty lines at start/end
    while old_stripped and not old_stripped[0].strip():
        old_stripped.pop(0)
    while old_stripped and not old_stripped[-1].strip():
        old_stripped.pop()

    if not old_stripped:
        return None

    n = len(old_stripped)

    # Pass 1: trailing-whitespace-tolerant (preserves leading indent check)
    best: tuple[int, int] | None = None
    best_distance = float('inf')
    for i in range(len(content_lines) - n + 1):
        window = [content_lines[i + j].rstrip() for j in range(n)]
        if window == old_stripped:
            distance = abs(i - (hint_line - 1))
            if distance < best_distance:
                best_distance = distance
                best = (i, n)
    if best is not None:
        return best

    # Pass 2: fully-stripped fallback (handles indentation mismatch from Gemini)
    old_fully_stripped = [l.strip() for l in old_stripped]
    best_distance = float('inf')
    for i in range(len(content_lines) - n + 1):
        window = [content_lines[i + j].strip() for j in range(n)]
        if window == old_fully_stripped:
            distance = abs(i - (hint_line - 1))
            if distance < best_distance:
                best_distance = distance
                best = (i, n)
    if best is not None:
        return best

    # Pass 3: fuzzy fallback near the hinted location for minor model drift
    max_start = len(content_lines) - n
    if max_start < 0:
        return None
    search_start = max(0, hint_line - 1 - 25)
    search_end = min(max_start, hint_line - 1 + 25)
    best_score = 0.0
    old_joined = "\n".join(old_fully_stripped)
    for i in range(search_start, max(search_start, search_end) + 1):
        window = [content_lines[i + j].strip() for j in range(n)]
        score = difflib.SequenceMatcher(None, old_joined, "\n".join(window)).ratio()
        if score > best_score:
            best_score = score
            best = (i, n)

    if best is not None and best_score >= 0.94:
        return best

    return best


def _find_and_replace(content: str, old_code: str, new_code: str,
                      hint_line: int) -> tuple[str, bool]:
    """Find old_code in content (whitespace-tolerant) and replace with new_code.

    Returns (new_content, success).
    """
    # Normalize line endings
    content = content.replace('\r\n', '\n')
    old_code = old_code.replace('\r\n', '\n')
    new_code = new_code.replace('\r\n', '\n')

    # 1. Exact match — fastest path (skip if duplicates, use line-match for precision)
    if old_code in content and content.count(old_code) == 1:
        return content.replace(old_code, new_code, 1), True

    # 2. Line-by-line match with trailing whitespace tolerance
    match = _line_match(content, old_code, hint_line)
    if match:
        start_idx, n = match
        lines = content.split('\n')
        replacement = new_code.split('\n') if new_code else []
        lines[start_idx:start_idx + n] = replacement
        return '\n'.join(lines), True

    return content, False


def apply_local(findings: list, repo_path: str = ".", file_filter: str | None = None) -> int:
    """Apply suggestion fixes to local files using text search-and-replace."""
    by_file: dict[str, list] = {}
    for f in findings:
        if f.suggestion is None or not f.old_code:
            continue
        if f.suggestion and _is_natural_language(f.suggestion):
            continue
        if _is_natural_language(f.old_code):
            continue
        # Safety: limit deletion-only operations to 50 lines
        if not f.suggestion and len(f.old_code.strip().splitlines()) > 50:
            continue
        if file_filter and f.file != file_filter:
            continue
        by_file.setdefault(f.file, []).append(f)

    if not by_file:
        print("  No applicable suggestions found.")
        return 0

    applied = 0
    for filepath, file_findings in by_file.items():
        full_path = os.path.join(repo_path, filepath)
        if not os.path.exists(full_path):
            print(f"  SKIP {filepath} (file not found locally)")
            continue

        with open(full_path, "r", encoding="utf-8") as fh:
            content = fh.read()

        # Normalize line endings
        content = content.replace('\r\n', '\n')

        # Sort by line descending so later replacements don't shift earlier ones
        file_applied = 0
        for f in sorted(file_findings, key=lambda x: x.line, reverse=True):
            content, ok = _find_and_replace(content, f.old_code, f.suggestion, f.line)
            if ok:
                file_applied += 1
            else:
                print(f"  SKIP {filepath}:{f.line} (code not found)")

        if file_applied:
            with open(full_path, "w", encoding="utf-8", newline='\n') as fh:
                fh.write(content)
            print(f"  Applied {file_applied} fix(es) to {filepath}")
            applied += file_applied

    return applied


# ============================================================
#  Pretty print
# ============================================================

def _format_elapsed(seconds: float) -> str:
    """Format seconds as '45s' or '2m 34s'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def print_review(
    review: ReviewSummary,
    applied: int = 0,
    elapsed: float = 0,
    *,
    show_usage: bool = True,
):
    fixable = sum(1 for f in review.findings if f.suggestion and f.old_code)
    non_fixable = len(review.findings) - fixable
    print(f"\n  {'='*56}")
    parts = [f"BoomAI Review — {len(review.findings)} issues"]
    if applied:
        parts.append(f"{applied} fixes applied")
    if elapsed:
        parts.append(_format_elapsed(elapsed))
    print(f"  {' | '.join(parts)}")
    if show_usage and review.usage and review.usage.api_calls > 0:
        from boomai.review.estimator import format_actual_cost
        print(format_actual_cost(review.usage))
        if len(review.usage.per_model) > 1:
            mixes = ", ".join(
                f"{model} x{bucket['api_calls']}"
                for model, bucket in review.usage.per_model.items()
            )
            print(f"  Models: {mixes}")
    print(f"  Findings: {fixable} fixable | {non_fixable} non-fixable")
    print(f"  {'='*56}")
    print(f"\n  {review.summary}\n")

    if not review.findings:
        print("  No issues found!")
        return

    for i, f in enumerate(review.findings, 1):
        has_fix = " [FIX]" if f.suggestion else ""
        print(f"  #{i} {f.file}:{f.line}{has_fix}")
        for line in f.body.split("\n")[:3]:
            print(f"      {line}")
        print()


# ============================================================
#  Command: fix
# ============================================================

def collect_files(repo_path: str = ".", exclude: list[str] | None = None,
                  include: list[str] | None = None) -> list[str]:
    """Collect all tracked source files using git ls-files (respects .gitignore)."""
    cmd = ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, check=True, cwd=repo_path,
        )
        files = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = _walk_files(repo_path)

    files = [f.strip() for f in files if f.strip()]

    if include:
        files = [f for f in files
                 if any(f.startswith(p) for p in include)]

    if exclude:
        files = [f for f in files
                 if not any(f.startswith(ex) or f == ex for ex in exclude)]

    return files


def _walk_files(repo_path: str) -> list[str]:
    """Fallback: walk directory tree, skipping common non-source dirs."""
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__",
                 "dist", "build", ".next", ".nuxt", "target", "bin", "obj"}
    result = []
    for root, dirs, filenames in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in filenames:
            rel = os.path.relpath(os.path.join(root, name), repo_path)
            result.append(rel.replace("\\", "/"))
    return result


def read_file_contents(
    files: list[str], repo_path: str = "."
) -> list[tuple[str, str]]:
    """Read file contents in parallel, returning (path, content) pairs."""
    def _read_one(filepath: str) -> tuple[str, str] | None:
        full_path = os.path.join(repo_path, filepath)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            if "\x00" in text[:8192]:
                return None
            return (filepath, text)
        except OSError:
            return None

    max_workers = min(32, max(4, (os.cpu_count() or 8) * 2))
    contents: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for result in executor.map(_read_one, files):
            if result is not None:
                contents.append(result)
    return contents


def _report_unreadable_files(
    requested_files: list[str],
    file_contents: list[tuple[str, str]],
    *,
    debug: bool = False,
) -> None:
    """Report files that were considered reviewable but could not be read."""
    loaded = {path for path, _ in file_contents}
    missing = [path for path in requested_files if path not in loaded]
    if not missing:
        return

    print(f"    Skipped {len(missing)} unreadable/binary file(s).")
    if debug:
        for path in missing[:10]:
            print(f"      - {path}")
        if len(missing) > 10:
            print(f"      ... and {len(missing) - 10} more")


def _apply_scan_profile(profile: str) -> None:
    """Apply a runtime scan profile without changing persisted settings."""
    base = {
        "max_scan_chars": settings.max_scan_chars,
        "scan_max_files_per_chunk": settings.scan_max_files_per_chunk,
        "patch_max_findings_per_chunk": settings.patch_max_findings_per_chunk,
        "prompt_pack_scan_max_extras": settings.prompt_pack_scan_max_extras,
        "prompt_pack_fix_max_extras": settings.prompt_pack_fix_max_extras,
    }
    deep = {
        "max_scan_chars": settings.deep_max_scan_chars,
        "scan_max_files_per_chunk": settings.deep_scan_max_files_per_chunk,
        "patch_max_findings_per_chunk": settings.deep_patch_max_findings_per_chunk,
        "prompt_pack_scan_max_extras": settings.deep_prompt_pack_scan_max_extras,
        "prompt_pack_fix_max_extras": settings.deep_prompt_pack_fix_max_extras,
    }
    selected = deep if profile == "deep" else base
    for key, value in selected.items():
        setattr(settings, key, value)
    settings.scan_profile = profile


async def run_local_scan(repo_path: str = ".",
                         exclude: list[str] | None = None,
                         include: list[str] | None = None,
                         comments: bool = False,
                         on_chunk_done=None,
                         file_contents: list[tuple[str, str]] | None = None,
                         issue_seeds=None,
                         code_index=None,
                         ) -> ReviewSummary:
    """Scan entire codebase and return AI review.

    If file_contents is provided, skips file collection/reading
    (used when caller already read files for estimation).
    """
    from boomai.review.gemini_review import scan_with_gemini

    if file_contents is None:
        # Collect and read files (standalone usage without estimation)
        print(f"\n  Collecting files...")
        if repo_path != ".":
            print(f"    Repo: {repo_path}")
        if include:
            print(f"    Include: {', '.join(include)}")
        if exclude:
            print(f"    Excluding: {', '.join(exclude)}")

        all_files = collect_files(repo_path, exclude=exclude, include=include)
        reviewable = filter_reviewable_files(all_files)
        languages = detect_languages(all_files)

        lang_str = ', '.join(languages) if languages else 'none detected'
        print(f"    {len(all_files):,} total, {len(reviewable)} reviewable ({lang_str})")

        if not reviewable:
            return ReviewSummary(
                summary="No reviewable source files found.",
                findings=[],
                critical_count=0,
                has_critical=False,
            )

        if len(reviewable) > settings.scan_max_files:
            print(f"    Warning: {len(reviewable)} files exceeds limit of {settings.scan_max_files}.")
            print(f"    Use --include to narrow scope.")
            return ReviewSummary(
                summary=f"Scan aborted: {len(reviewable)} files exceeds limit of {settings.scan_max_files}.",
                findings=[],
                critical_count=0,
                has_critical=False,
            )

        file_contents = read_file_contents(reviewable, repo_path)
        _report_unreadable_files(reviewable, file_contents, debug=settings.scan_debug)
        languages = detect_languages([p for p, _ in file_contents])
    else:
        languages = detect_languages([p for p, _ in file_contents])

    total_chars = sum(len(c) for _, c in file_contents)
    print(f"    {total_chars:,} chars across {len(file_contents)} files")
    print(f"    Model: {settings.llm_model}")

    def _progress(msg: str) -> None:
        print(f"  {msg}")

    return await scan_with_gemini(
        file_contents=file_contents,
        detected_languages=languages,
        comments=comments,
        on_progress=_progress,
        on_chunk_done=on_chunk_done,
        issue_seeds=issue_seeds,
        code_index=code_index,
    )


def cmd_fix(args):
    """Scan entire codebase and auto-apply fixes."""
    from boomai.review.estimator import estimate_scan, format_estimate, get_pricing
    from boomai.review.estimation_history import record_run
    from boomai.review.run_cost_report import write_run_cost_report

    require_api_key()
    profile = "deep" if getattr(args, "deep", False) else getattr(args, "profile", settings.scan_profile)
    _apply_scan_profile(profile)
    cost_reporting_enabled = settings.cost_reporting_enabled
    if getattr(args, "cost_report", False):
        cost_reporting_enabled = True
    if getattr(args, "clean_run", False):
        cost_reporting_enabled = False
    repo_path = os.path.abspath(".")

    # ── Collect files ─────────────────────────────────────
    print(f"\n  Collecting files...")
    all_files = collect_files(repo_path)
    if args.shallow:
        all_files = [f for f in all_files if "/" not in f]
    reviewable = filter_reviewable_files(all_files)
    languages = detect_languages(all_files)

    lang_str = ", ".join(languages) if languages else "none detected"
    print(f"    {len(all_files):,} total, {len(reviewable)} reviewable ({lang_str})")
    print(f"    Profile: {settings.scan_profile}")

    if not reviewable:
        print("  No reviewable source files found.")
        return

    if len(reviewable) > settings.scan_max_files:
        print(f"    Warning: {len(reviewable)} files exceeds limit of {settings.scan_max_files}.")
        print(f"    Use --include to narrow scope.")
        return

    print("    Reading file contents...")
    file_contents = read_file_contents(reviewable, repo_path)
    _report_unreadable_files(reviewable, file_contents, debug=settings.scan_debug)

    # ── Estimate cost & time ──────────────────────────────
    estimate = estimate_scan(
        file_contents=file_contents,
        model=settings.llm_model,
        patch_model=settings.patch_llm_model,
        max_scan_chars=settings.max_scan_chars,
        scan_output_tokens=settings.scan_output_tokens,
        plan_output_tokens=settings.plan_output_tokens,
        profile=settings.scan_profile,
        patch_max_findings_per_chunk=settings.patch_max_findings_per_chunk,
        languages=languages,
    )
    format_estimate(estimate)
    
    while True:
        try:
            answer = input("  Proceed? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return
        if answer in ("", "y", "yes"):
            break
        if answer in ("n", "no"):
            print("  Aborted.")
            return
        print("  Please enter Y or n.")

    # ── Scan ──────────────────────────────────────────────
    print("  Building code index...")
    code_index = build_code_index(file_contents, languages)
    t0 = time.monotonic()
    comments = settings.scan_comments
    analysis = run_static_analysis_suite(
        repo_path=repo_path,
        reviewable_files=reviewable,
        detected_languages=languages,
        on_progress=lambda msg: print(f"  {msg}"),
    )

    applied_total = 0

    def _on_chunk_done(chunk_review):
        nonlocal applied_total
        if chunk_review.findings:
            print(f"\n  Applying fixes...")
            count = apply_local(chunk_review.findings, repo_path)
            applied_total += count

    try:
        review = asyncio.run(run_local_scan(
            repo_path, comments=comments,
            on_chunk_done=_on_chunk_done,
            file_contents=file_contents,
            issue_seeds=analysis.prioritized_issue_seeds,
            code_index=code_index,
        ))
    except BaseException as exc:
        print(f"\n  Fatal scan error: {type(exc).__name__}: {exc}")
        if settings.scan_debug:
            traceback.print_exc()
        return
    elapsed = time.monotonic() - t0
    cost_report_path = None
    if cost_reporting_enabled and review.usage and review.usage.api_calls > 0:
        record_run(
            features=estimate.features,
            elapsed_seconds=elapsed,
            usage=review.usage,
            findings_count=len(review.findings),
            applied_count=applied_total,
            get_pricing=get_pricing,
        )
        cost_report_path = write_run_cost_report(
            repo_path=repo_path,
            estimate=estimate,
            review=review,
            elapsed_seconds=elapsed,
            applied_count=applied_total,
            issue_seed_count=len(analysis.prioritized_issue_seeds),
            languages=languages,
        )
    print_review(review, applied=applied_total, elapsed=elapsed, show_usage=cost_reporting_enabled)
    if cost_report_path is not None:
        print(f"  Cost report: {cost_report_path}")

    if applied_total:
        print(f"  Run `git diff` to see changes.")


# ============================================================
#  API key / settings management
# ============================================================

GLOBAL_ENV_DIR = Path.home() / ".boomai"
GLOBAL_ENV_FILE = GLOBAL_ENV_DIR / ".env"


def _save_setting(env_key: str, value: str):
    """Save a BOOMAI_* setting to ~/.boomai/.env"""
    GLOBAL_ENV_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    if GLOBAL_ENV_FILE.exists():
        lines = GLOBAL_ENV_FILE.read_text(encoding="utf-8").splitlines()
    new_lines = [l for l in lines if not l.startswith(f"{env_key}=")]
    new_lines.append(f"{env_key}={value}")
    GLOBAL_ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def require_api_key():
    if settings.google_api_key:
        return
    print("\n  No API key found!")
    print("  Get one at: https://aistudio.google.com/apikey\n")
    key = input("  Enter your Gemini API key: ").strip()
    if not key:
        print("  Error: No key provided.")
        sys.exit(1)
    _save_setting("BOOMAI_GOOGLE_API_KEY", key)
    settings.google_api_key = key
    print(f"  Key saved to {GLOBAL_ENV_FILE}")
    print()


def cmd_settings(args):
    """Interactive settings menu."""
    while True:
        key = settings.google_api_key
        if key:
            masked = key[:8] + "..." + key[-4:] if len(key) > 12 else "***"
        else:
            masked = "not set"

        comments_str = "ON" if settings.scan_comments else "OFF"
        reporting_str = "ON" if settings.cost_reporting_enabled else "OFF"

        print(f"\n  BoomAI Settings")
        print(f"  {'=' * 36}")
        print(f"  [1] Gemini API Key      {masked}")
        print(f"  [2] Inline comments     {comments_str}")
        print(f"  [4] Generate detailed cost report  {reporting_str}")
        print()

        try:
            choice = input("  Enter number to change (q to quit): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice == "q" or choice == "":
            break
        elif choice == "1":
            new_key = input("  Enter new Gemini API key: ").strip()
            if new_key:
                _save_setting("BOOMAI_GOOGLE_API_KEY", new_key)
                settings.google_api_key = new_key
                print(f"  Key saved.")
        elif choice == "2":
            new_val = not settings.scan_comments
            _save_setting("BOOMAI_SCAN_COMMENTS", str(new_val).lower())
            settings.scan_comments = new_val
            print(f"  Inline comments: {'ON' if new_val else 'OFF'}")
        elif choice == "4":
            new_val = not settings.cost_reporting_enabled
            _save_setting("BOOMAI_COST_REPORTING_ENABLED", str(new_val).lower())
            settings.cost_reporting_enabled = new_val
            print(f"  Generate detailed cost report: {'ON' if new_val else 'OFF'}")


# ============================================================
#  Main CLI
# ============================================================

def main():
    # Prevent UnicodeEncodeError on Windows consoles (e.g. cp1253)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")

    parser = argparse.ArgumentParser(
        prog="boomai",
        description="BoomAI — AI-powered code fixer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples (run from inside your project):
  boomai fix                      # scan + auto-fix codebase
  boomai fix --deep               # deeper, slower scan for higher coverage
  boomai settings                 # configure API key & preferences
""",
    )
    sub = parser.add_subparsers(dest="command")

    # --- fix ---
    fix_parser = sub.add_parser("fix", help="Scan codebase and auto-apply fixes")
    fix_parser.add_argument(
        "--shallow", action="store_true",
        help="Only scan files in CWD, skip subdirectories",
    )
    fix_parser.add_argument(
        "--profile",
        choices=("default", "deep"),
        default=settings.scan_profile,
        help="Scan profile: default is faster/cheaper, deep spends more to increase coverage.",
    )
    fix_parser.add_argument(
        "--deep",
        action="store_true",
        help="Shortcut for --profile deep.",
    )
    fix_parser.add_argument(
        "--cost-report",
        action="store_true",
        help="Force detailed cost/reporting output for this run.",
    )
    fix_parser.add_argument(
        "--clean-run",
        action="store_true",
        help="Run without cost line, history write, or cost-report artifact.",
    )

    # --- settings ---
    sub.add_parser("settings", help="Configure API key & preferences")

    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    print_banner()

    if args.command == "fix":
        cmd_fix(args)
    elif args.command == "settings":
        cmd_settings(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
