"""
BoomAI CLI — AI-powered code fixer

Usage (from inside your project):
  boom-ai fix                     # scan + auto-fix codebase
  boom-ai settings                # configure API key & preferences
"""

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from boomai.config import settings
from boomai.languages import detect_languages, filter_reviewable_files
from boomai.models import ReviewSummary

logger = logging.getLogger(__name__)


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


def print_review(review: ReviewSummary, applied: int = 0, elapsed: float = 0):
    print(f"\n  {'='*56}")
    parts = [f"BoomAI Review — {len(review.findings)} issues"]
    if applied:
        parts.append(f"{applied} fixes applied")
    if elapsed:
        parts.append(_format_elapsed(elapsed))
    print(f"  {' | '.join(parts)}")
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
    """Read file contents, returning (path, content) pairs. Skips binary files."""
    contents = []
    for filepath in files:
        full_path = os.path.join(repo_path, filepath)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            if "\x00" in text[:8192]:
                continue
            contents.append((filepath, text))
        except OSError:
            continue
    return contents


async def run_local_scan(repo_path: str = ".",
                         exclude: list[str] | None = None,
                         include: list[str] | None = None,
                         comments: bool = False,
                         explanations: bool = True,
                         on_chunk_done=None) -> ReviewSummary:
    """Scan entire codebase and return AI review."""
    from boomai.gemini_review import scan_with_gemini

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

    # Read file contents
    file_contents = read_file_contents(reviewable, repo_path)
    total_chars = sum(len(c) for _, c in file_contents)
    print(f"    {total_chars:,} chars across {len(file_contents)} files")
    print(f"    Model: {settings.llm_model}")

    def _progress(msg: str) -> None:
        print(f"  {msg}")

    return await scan_with_gemini(
        file_contents=file_contents,
        detected_languages=languages,
        comments=comments,
        explanations=explanations,
        on_progress=_progress,
        on_chunk_done=on_chunk_done,
    )


def cmd_fix(args):
    """Scan entire codebase and auto-apply fixes."""
    require_api_key()
    t0 = time.monotonic()
    repo_path = os.path.abspath(".")
    comments = settings.scan_comments
    explanations = settings.scan_explanations

    applied_total = 0

    def _on_chunk_done(chunk_review):
        nonlocal applied_total
        if chunk_review.findings:
            print(f"\n  Applying fixes...")
            count = apply_local(chunk_review.findings, repo_path)
            applied_total += count

    review = asyncio.run(run_local_scan(
        repo_path, comments=comments, explanations=explanations,
        on_chunk_done=_on_chunk_done,
    ))
    elapsed = time.monotonic() - t0
    print_review(review, applied=applied_total, elapsed=elapsed)

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
        explanations_str = "ON" if settings.scan_explanations else "OFF"

        print(f"\n  BoomAI Settings")
        print(f"  {'=' * 36}")
        print(f"  [1] Gemini API Key      {masked}")
        print(f"  [2] Inline comments     {comments_str}")
        print(f"  [3] Explanations        {explanations_str}")
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
        elif choice == "3":
            new_val = not settings.scan_explanations
            _save_setting("BOOMAI_SCAN_EXPLANATIONS", str(new_val).lower())
            settings.scan_explanations = new_val
            print(f"  Explanations: {'ON' if new_val else 'OFF'}")


# ============================================================
#  Main CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        prog="boom-ai",
        description="BoomAI — AI-powered code fixer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples (run from inside your project):
  boom-ai fix                     # scan + auto-fix codebase
  boom-ai settings                # configure API key & preferences
""",
    )
    sub = parser.add_subparsers(dest="command")

    # --- fix ---
    sub.add_parser("fix", help="Scan codebase and auto-apply fixes")

    # --- settings ---
    sub.add_parser("settings", help="Configure API key & preferences")

    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    if args.command == "fix":
        cmd_fix(args)
    elif args.command == "settings":
        cmd_settings(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
