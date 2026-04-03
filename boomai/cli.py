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
import re
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
from boomai.core.google_models import apply_runtime_models, get_runtime_models
from boomai.core.models import ReviewSummary
from boomai.review.progress_history import ChunkProgressFeatures, predict_chunk_elapsed_seconds

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
    search_start = min(max_start, max(0, hint_line - 1 - 25))
    search_end = max(search_start, min(max_start, hint_line - 1 + 25))
    best_score = 0.0
    old_joined = "\n".join(old_fully_stripped)
    for i in range(search_start, search_end + 1):
        window = [content_lines[i + j].strip() for j in range(n)]
        score = difflib.SequenceMatcher(None, old_joined, "\n".join(window)).ratio()
        if score > best_score:
            best_score = score
            best = (i, n)

    if best is not None and best_score >= 0.94:
        return best

    return None


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


class _ScanProgressDisplay:
    """Verbose debug logs or a compact progress bar, depending on scan_debug."""

    def __init__(self, *, debug: bool, total_files: int = 0, scan_model: str = "", profile: str = "default"):
        self.debug = debug
        self.total_files = max(0, int(total_files))
        self.scan_model = scan_model
        self.profile = profile
        self.total_chunks = 0
        self.total_weight = 0.0
        self.completed_weight = 0.0
        self.completed_files = 0
        self.completed_labels: set[str] = set()
        self.active_labels: set[str] = set()
        self.label_file_counts: dict[str, int] = {}
        self.label_char_counts: dict[str, int] = {}
        self.label_started_at: dict[str, float] = {}
        self.label_expected_seconds: dict[str, float] = {}
        self._bar_visible = False
        self._spinner_frames = "|/-\\"
        self._spinner_index = 0

    @staticmethod
    def _normalize_label(raw_label: str) -> str:
        return raw_label.strip().replace("[", "").replace("]", "")

    def _label_weight(self, label: str) -> float:
        parts = label.split("/")
        depth = max(0, len(parts) - 2)
        return 1.0 / (2 ** depth)

    @staticmethod
    def _fmt_units(value: float) -> str:
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        return f"{value:.1f}"

    def _clear_bar(self) -> None:
        if self.debug:
            return
        if self._bar_visible:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._bar_visible = False

    def _render_bar(self) -> None:
        if self.debug:
            return
        if self.total_files > 0:
            total_units = float(self.total_files)
            estimated_units = float(self.completed_files)
            now = time.monotonic()
            for label in self.active_labels:
                file_count = self.label_file_counts.get(label, 0)
                started_at = self.label_started_at.get(label, now)
                expected = self.label_expected_seconds.get(label, 0.0)
                if file_count <= 0 or expected <= 0:
                    continue
                active_ratio = min(0.95, max(0.0, (now - started_at) / expected))
                estimated_units += file_count * active_ratio
            completed_units = min(total_units, estimated_units)
            suffix = f" (est. {completed_units:.0f}/{self.total_files} files)"
        else:
            if self.total_weight <= 0:
                return
            total_units = self.total_weight
            completed_units = self.completed_weight
            suffix = ""
        width = 28
        active_chunks = len(self.active_labels)
        ratio = completed_units / total_units
        ratio = min(1.0, ratio)
        filled = int(round(width * ratio))
        bar = "#" * filled + "." * (width - filled)
        percent = int(round(ratio * 100))
        spinner = ""
        if active_chunks and completed_units < total_units:
            spinner = f" {self._spinner_frames[self._spinner_index % len(self._spinner_frames)]}"
            self._spinner_index += 1
        sys.stdout.write(
            f"\r  Scan progress: [{bar}] {percent:3d}%{suffix}{spinner}"
        )
        sys.stdout.flush()
        self._bar_visible = True

    def emit(self, msg: str) -> None:
        plain = msg.strip()
        if self.debug:
            print(f"  {plain}")
            return

        if plain in {"Planning review chunks...", "Using greedy chunking"}:
            self._clear_bar()
            print(f"  {plain}")
            return

        planned = re.match(r"(\d+) chunk\(s\) planned, model: (.+)", plain)
        if planned:
            self._clear_bar()
            self.total_chunks = int(planned.group(1))
            self.total_weight = float(self.total_chunks)
            self.completed_weight = 0.0
            self.completed_files = 0
            self.completed_labels.clear()
            self.active_labels.clear()
            self.label_file_counts.clear()
            print(f"  Planned {self.total_chunks} review chunks")
            return

        if plain.startswith("Reviewing code"):
            self._clear_bar()
            print("  Reviewing code...")
            return

        chunk_start = re.match(r"(\[\d+/\d+\](?:/[A-Za-z0-9_-]+)*)\s+(\d+)\s+files,\s+([\d,]+)\s+chars\.\.\.", plain)
        if chunk_start:
            label = self._normalize_label(chunk_start.group(1))
            file_count = int(chunk_start.group(2))
            char_count = int(chunk_start.group(3).replace(",", ""))
            self.active_labels.add(label)
            self.label_file_counts[label] = file_count
            self.label_char_counts[label] = char_count
            self.label_started_at[label] = time.monotonic()
            split_depth = max(0, len(label.split("/")) - 2)
            predicted_seconds, _ = predict_chunk_elapsed_seconds(
                ChunkProgressFeatures(
                    chunk_chars=char_count,
                    file_count=file_count,
                    split_depth=split_depth,
                    scan_model_flash=int("flash" in self.scan_model.lower()),
                    profile_deep=int(self.profile == "deep"),
                )
            )
            self.label_expected_seconds[label] = predicted_seconds
            self._render_bar()
            return

        split = re.match(r"(\[\d+/\d+\](?:/[A-Za-z0-9_-]+)*)\s+failed .*splitting", plain)
        if split:
            label = self._normalize_label(split.group(1))
            self.active_labels.discard(label)
            self.label_started_at.pop(label, None)
            self.label_expected_seconds.pop(label, None)
            self._render_bar()
            return

        completed = re.match(r"(\[\d+/\d+\](?:/[A-Za-z0-9_-]+)*)\s+completed$", plain)
        if completed:
            label = self._normalize_label(completed.group(1))
            if label not in self.completed_labels:
                self.completed_labels.add(label)
                self.completed_weight = min(self.total_weight, self.completed_weight + self._label_weight(label))
                if self.total_files > 0:
                    self.completed_files = min(
                        self.total_files,
                        self.completed_files + self.label_file_counts.get(label, 0),
                    )
            self.active_labels.discard(label)
            self.label_started_at.pop(label, None)
            self.label_expected_seconds.pop(label, None)
            self._render_bar()
            if (
                (self.total_files > 0 and self.completed_files >= self.total_files)
                or (self.total_files <= 0 and self.completed_weight >= self.total_weight)
            ):
                self.finish()
            return

        heartbeat = re.match(r"\[(\d+)/(\d+)\](?:/[A-Za-z0-9_-]+)?\.\.\. \(\d+s\)", plain)
        if heartbeat:
            self._render_bar()
            return

        if "patching " in plain:
            self._render_bar()
            return

        if plain.startswith("Done") or "rate limited" in plain.lower():
            self._clear_bar()
            print(f"  {plain}")
            return

    def chunk_done(self, _chunk_review: ReviewSummary) -> None:
        return

    def finish(self) -> None:
        self._clear_bar()


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
        print(
            f"  Stats: {review.usage.api_calls} API calls | "
            f"{review.usage.prompt_tokens:,} input | {review.usage.completion_tokens:,} output"
        )
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


def _normalize_repo_target(target: str, repo_path: str) -> str | None:
    """Normalize a CLI target path into a repo-relative POSIX path."""
    repo_root = Path(repo_path).resolve()
    target_path = Path(target)

    try:
        resolved = target_path.resolve() if target_path.is_absolute() else (repo_root / target_path).resolve()
    except OSError:
        return None

    try:
        relative = resolved.relative_to(repo_root)
    except ValueError:
        return None

    rel = relative.as_posix().strip("/")
    return rel


def _select_target_files(
    all_files: list[str],
    repo_path: str,
    targets: list[str],
) -> tuple[list[str], list[str]]:
    """Select tracked files matching explicit file/folder targets."""
    if not targets:
        return all_files, []

    selected: list[str] = []
    unmatched: list[str] = []
    known_files = set(all_files)

    for raw_target in targets:
        normalized = _normalize_repo_target(raw_target, repo_path)
        if normalized is None:
            unmatched.append(raw_target)
            continue

        if normalized == "":
            for path in all_files:
                if path not in selected:
                    selected.append(path)
            continue

        if normalized in known_files:
            if normalized not in selected:
                selected.append(normalized)
            continue

        prefix = normalized.rstrip("/") + "/"
        matches = [path for path in all_files if path.startswith(prefix)]
        if matches:
            for path in matches:
                if path not in selected:
                    selected.append(path)
            continue

        unmatched.append(raw_target)

    return selected, unmatched


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
                         runtime_models=None,
                         comments: bool = False,
                         on_chunk_done=None,
                         on_progress=None,
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
    scan_model = runtime_models.strong_model_id if runtime_models else settings.strong_model
    print(f"    {total_chars:,} chars across {len(file_contents)} files")
    print(f"    Model: {scan_model}")

    def _progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        else:
            print(f"  {msg}")

    return await scan_with_gemini(
        file_contents=file_contents,
        detected_languages=languages,
        runtime_models=runtime_models,
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
    runtime_models = get_runtime_models()
    apply_runtime_models(runtime_models)
    detailed_cost_report_enabled = settings.cost_reporting_enabled
    if getattr(args, "cost_report", False):
        detailed_cost_report_enabled = True
    if getattr(args, "clean_run", False):
        detailed_cost_report_enabled = False
    repo_path = os.path.abspath(".")

    # ── Collect files ─────────────────────────────────────
    print(f"\n  Collecting files...")
    all_files = collect_files(repo_path)
    if args.shallow:
        all_files = [f for f in all_files if "/" not in f]
    selected_files, unmatched_targets = _select_target_files(all_files, repo_path, args.targets)
    if args.targets:
        print(f"    Targets: {', '.join(args.targets)}")
    if unmatched_targets:
        print(f"    Warning: no matches for {', '.join(unmatched_targets)}")
    all_files = selected_files
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
        model=runtime_models.strong_model_id,
        patch_model=runtime_models.weak_model_id,
        model_label=runtime_models.strong_display_name,
        patch_model_label=runtime_models.weak_display_name,
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
    progress_display = _ScanProgressDisplay(
        debug=settings.scan_debug,
        total_files=len(file_contents),
        scan_model=runtime_models.strong_model_id,
        profile=settings.scan_profile,
    )
    analysis = run_static_analysis_suite(
        repo_path=repo_path,
        reviewable_files=reviewable,
        detected_languages=languages,
        on_progress=lambda msg: print(f"  {msg}"),
    )

    try:
        review = asyncio.run(run_local_scan(
            repo_path, comments=comments,
            runtime_models=runtime_models,
            on_progress=progress_display.emit,
            file_contents=file_contents,
            issue_seeds=analysis.prioritized_issue_seeds,
            code_index=code_index,
        ))
    except BaseException as exc:
        progress_display.finish()
        print(f"\n  Fatal scan error: {type(exc).__name__}: {exc}")
        if settings.scan_debug:
            traceback.print_exc()
        return
    progress_display.finish()
    applied_total = 0
    if review.findings:
        print(f"\n  Applying fixes...")
        applied_total = apply_local(review.findings, repo_path)
    elapsed = time.monotonic() - t0
    cost_report_path = None
    if review.usage and review.usage.api_calls > 0:
        record_run(
            features=estimate.features,
            elapsed_seconds=elapsed,
            usage=review.usage,
            findings_count=len(review.findings),
            applied_count=applied_total,
            get_pricing=get_pricing,
        )
    if detailed_cost_report_enabled and review.usage and review.usage.api_calls > 0:
        cost_report_path = write_run_cost_report(
            repo_path=repo_path,
            estimate=estimate,
            review=review,
            runtime_models=runtime_models,
            elapsed_seconds=elapsed,
            applied_count=applied_total,
            issue_seed_count=len(analysis.prioritized_issue_seeds),
            languages=languages,
        )
    print_review(review, applied=applied_total, elapsed=elapsed, show_usage=True)
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


def _unset_setting(env_key: str):
    """Remove a BOOMAI_* setting from ~/.boomai/.env if present."""
    if not GLOBAL_ENV_FILE.exists():
        return
    lines = GLOBAL_ENV_FILE.read_text(encoding="utf-8").splitlines()
    new_lines = [line for line in lines if not line.startswith(f"{env_key}=")]
    if new_lines:
        GLOBAL_ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    else:
        GLOBAL_ENV_FILE.write_text("", encoding="utf-8")


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


def _mask_api_key(key: str) -> str:
    if not key:
        return "not set"
    return key[:8] + "..." + key[-4:] if len(key) > 12 else "***"


def _format_model_choice(entry) -> str:
    alias_suffix = " [alias]" if getattr(entry, "is_alias", False) else ""
    return f"{entry.display_name} [{entry.model_id}]{alias_suffix}"


def _set_model_role(role: str, *, mode: str, override: str = "") -> None:
    mode_env = f"BOOMAI_{role.upper()}_MODEL_MODE"
    override_env = f"BOOMAI_{role.upper()}_MODEL_OVERRIDE"
    if mode == "auto":
        _save_setting(mode_env, "auto")
        _unset_setting(override_env)
        setattr(settings, f"{role}_model_mode", "auto")
        setattr(settings, f"{role}_model_override", "")
        return

    normalized_override = override.strip()
    if not normalized_override:
        return
    _save_setting(mode_env, "manual")
    _save_setting(override_env, normalized_override)
    setattr(settings, f"{role}_model_mode", "manual")
    setattr(settings, f"{role}_model_override", normalized_override)


def _pick_role_model(role: str, runtime_models) -> None:
    role_title = role.title()
    candidates = list(runtime_models.strong_candidates if role == "strong" else runtime_models.weak_candidates)
    current_model_id = runtime_models.strong_model_id if role == "strong" else runtime_models.weak_model_id
    current_mode = runtime_models.strong_mode if role == "strong" else runtime_models.weak_mode

    while True:
        print(f"\n  {role_title} model")
        print(f"  {'-' * 36}")
        print(f"  Current: {current_mode.upper()} -> {current_model_id}")
        print(f"  [0] Reset to AUTO")
        for index, entry in enumerate(candidates, start=1):
            marker = " (current)" if entry.model_id == current_model_id else ""
            print(f"  [{index}] {_format_model_choice(entry)}{marker}")
        print()
        choice = input("  Choose model (q to cancel): ").strip().lower()
        if choice in {"", "q"}:
            return
        if choice == "0":
            _set_model_role(role, mode="auto")
            print(f"  {role_title} model: AUTO")
            return
        if not choice.isdigit():
            print("  Enter a valid number.")
            continue
        index = int(choice)
        if index < 1 or index > len(candidates):
            print("  Enter a valid number.")
            continue
        selected = candidates[index - 1]
        _set_model_role(role, mode="manual", override=selected.model_id)
        print(f"  {role_title} model: {selected.display_name}")
        return


def cmd_settings(args):
    """Interactive settings menu."""
    while True:
        runtime_models = get_runtime_models()
        apply_runtime_models(runtime_models)
        masked = _mask_api_key(settings.google_api_key)
        comments_str = "ON" if settings.scan_comments else "OFF"
        debug_str = "ON" if settings.scan_debug else "OFF"
        reporting_str = "ON" if settings.cost_reporting_enabled else "OFF"

        print(f"\n  BoomAI Settings")
        print(f"  {'=' * 54}")
        print(f"  Catalog source: {runtime_models.source.upper()}")
        if runtime_models.catalog_error:
            print(f"  Note: using {runtime_models.source} catalog after refresh error.")
        print(f"  [1] Gemini API Key                    {masked}")
        print(
            f"  [2] Strong model ({runtime_models.strong_mode.upper()})"
            f"          {runtime_models.strong_display_name} [{runtime_models.strong_model_id}]"
        )
        print(
            f"  [3] Weak model ({runtime_models.weak_mode.upper()})"
            f"            {runtime_models.weak_display_name} [{runtime_models.weak_model_id}]"
        )
        print(f"  [4] Inline comments                  {comments_str}")
        print(f"  [5] Debug logs                       {debug_str}")
        print(f"  [6] Generate detailed cost report    {reporting_str}")
        print(f"  [7] Refresh model catalog now")
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
                runtime_models = get_runtime_models(force_refresh=True)
                apply_runtime_models(runtime_models)
        elif choice == "2":
            _pick_role_model("strong", runtime_models)
        elif choice == "3":
            _pick_role_model("weak", runtime_models)
        elif choice == "4":
            new_val = not settings.scan_comments
            _save_setting("BOOMAI_SCAN_COMMENTS", str(new_val).lower())
            settings.scan_comments = new_val
            print(f"  Inline comments: {'ON' if new_val else 'OFF'}")
        elif choice == "5":
            new_val = not settings.scan_debug
            _save_setting("BOOMAI_SCAN_DEBUG", str(new_val).lower())
            settings.scan_debug = new_val
            print(f"  Debug logs: {'ON' if new_val else 'OFF'}")
        elif choice == "6":
            new_val = not settings.cost_reporting_enabled
            _save_setting("BOOMAI_COST_REPORTING_ENABLED", str(new_val).lower())
            settings.cost_reporting_enabled = new_val
            print(f"  Generate detailed cost report: {'ON' if new_val else 'OFF'}")
        elif choice == "7":
            refreshed = get_runtime_models(force_refresh=True)
            apply_runtime_models(refreshed)
            print(
                f"  Refreshed model catalog: "
                f"strong={refreshed.strong_model_id}, weak={refreshed.weak_model_id} "
                f"({refreshed.source.upper()})"
            )


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
  boomai fix Assets/Scripts Game.cs  # only scan specific folders/files
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
    fix_parser.add_argument(
        "targets",
        nargs="*",
        help="Optional folders/files to scan, relative or absolute.",
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
