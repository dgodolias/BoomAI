"""
BoomAI CLI — AI-powered code review tool

Usage (from inside your project):
  boom-ai scan                          # full codebase scan
  boom-ai scan --apply                  # scan + auto-apply fixes
  boom-ai review analytics development  # diff-based review
  boom-ai apply-all analytics development
  boom-ai setup dgodolias/QuaR --branch development
"""

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from boomai.config import settings
from boomai.gemini_review import review_with_gemini
from boomai.languages import detect_languages, filter_reviewable_files
from boomai.models import ReviewSummary
from boomai.static_analysis import run_semgrep

logger = logging.getLogger(__name__)

# Package data directory (frozen copies for setup command)
DATA_DIR = Path(__file__).parent / "data"

# Files installed by BoomAI setup — auto-excluded from reviews
BOOMAI_EXCLUDES = [
    "scripts/",
    ".github/workflows/boomai.yml",
    "rules/",
    "requirements.txt",
]


# ============================================================
#  Git helpers
# ============================================================

def git_changed_files(base: str, head: str, repo_path: str = ".",
                      exclude: list[str] | None = None) -> list[str]:
    cmd = ["git", "diff", "--name-only", f"{base}...{head}"]
    if exclude:
        cmd.extend(["--", ".", *[f":(exclude){p}" for p in exclude]])
    result = subprocess.run(
        cmd, capture_output=True, check=True, cwd=repo_path,
    )
    output = result.stdout.decode("utf-8", errors="replace")
    return [f.strip() for f in output.strip().splitlines() if f.strip()]


def git_diff(base: str, head: str, repo_path: str = ".",
             exclude: list[str] | None = None) -> str:
    cmd = ["git", "diff", f"{base}...{head}"]
    if exclude:
        cmd.extend(["--", ".", *[f":(exclude){p}" for p in exclude]])
    result = subprocess.run(
        cmd, capture_output=True, check=True, cwd=repo_path,
    )
    return result.stdout.decode("utf-8", errors="replace")


def gh(*args) -> str:
    """Run gh CLI command and return stdout."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


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
    best: tuple[int, int] | None = None
    best_distance = float('inf')

    for i in range(len(content_lines) - n + 1):
        window = [content_lines[i + j].rstrip() for j in range(n)]
        if window == old_stripped:
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

    # 1. Exact match — fastest path
    if old_code in content:
        return content.replace(old_code, new_code, 1), True

    # 2. Line-by-line match with trailing whitespace tolerance
    match = _line_match(content, old_code, hint_line)
    if match:
        start_idx, n = match
        lines = content.split('\n')
        lines[start_idx:start_idx + n] = new_code.split('\n')
        return '\n'.join(lines), True

    return content, False


def apply_local(findings: list, repo_path: str = ".", file_filter: str | None = None) -> int:
    """Apply suggestion fixes to local files using text search-and-replace."""
    by_file: dict[str, list] = {}
    for f in findings:
        if not f.suggestion or not f.old_code:
            continue
        if _is_natural_language(f.suggestion) or _is_natural_language(f.old_code):
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

def print_review(review: ReviewSummary):
    print(f"\n{'='*60}")
    print(f"  BoomAI Review")
    print(f"{'='*60}")
    print(f"\n{review.summary}\n")

    if not review.findings:
        print("  No issues found!")
        return

    print(f"  {len(review.findings)} issue(s) found"
          f" ({review.critical_count} critical)\n")

    for i, f in enumerate(review.findings, 1):
        has_fix = " [FIX]" if f.suggestion else ""
        print(f"  #{i} {f.file}:{f.line}{has_fix}")
        for line in f.body.split("\n")[:3]:
            print(f"      {line}")
        print()


# ============================================================
#  Command: setup
# ============================================================

def cmd_setup(args):
    """Install BoomAI on a GitHub repo (plug-and-play)."""
    repo = args.repo
    branch = args.branch

    print(f"\n  BoomAI Setup: {repo} (branch: {branch})")
    print(f"{'='*60}")

    # 1. Set secret
    api_key = settings.google_api_key
    if not api_key:
        print("  Error: Set BOOMAI_GOOGLE_API_KEY in .env or as env var")
        return

    print("  [1/5] Setting BOOMAI_GOOGLE_API_KEY secret...")
    subprocess.run(
        ["gh", "secret", "set", "BOOMAI_GOOGLE_API_KEY", "-R", repo],
        input=api_key, text=True, check=True,
        capture_output=True,
    )

    # 2. Clone
    tmpdir = tempfile.mkdtemp()
    print(f"  [2/5] Cloning {repo}...")
    subprocess.run(
        ["gh", "repo", "clone", repo, os.path.join(tmpdir, "repo"), "--", "--quiet"],
        check=True, capture_output=True,
    )
    repo_dir = os.path.join(tmpdir, "repo")

    # 3. Create branch + copy files from package data
    print("  [3/5] Copying BoomAI files...")
    subprocess.run(["git", "checkout", branch], cwd=repo_dir,
                    check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "boomai/install"], cwd=repo_dir,
                    check=True, capture_output=True)

    for d in [".github/workflows", "scripts", "rules/semgrep"]:
        os.makedirs(os.path.join(repo_dir, d), exist_ok=True)

    # Source paths relative to DATA_DIR, destination in target repo
    files_to_copy = [
        ("workflows/boomai.yml",            ".github/workflows/boomai.yml"),
        ("scripts/__init__.py",             "scripts/__init__.py"),
        ("scripts/config.py",               "scripts/config.py"),
        ("scripts/models.py",               "scripts/models.py"),
        ("scripts/prompts.py",              "scripts/prompts.py"),
        ("scripts/languages.py",            "scripts/languages.py"),
        ("scripts/gemini_review.py",        "scripts/gemini_review.py"),
        ("scripts/github_client.py",        "scripts/github_client.py"),
        ("scripts/static_analysis.py",      "scripts/static_analysis.py"),
        ("scripts/slack_notifier.py",       "scripts/slack_notifier.py"),
        ("scripts/main.py",                 "scripts/main.py"),
        ("scripts/apply_fixes.py",          "scripts/apply_fixes.py"),
        ("semgrep/unity-rules.yml",         "rules/semgrep/unity-rules.yml"),
        ("requirements.txt",                "requirements.txt"),
    ]
    for src, dst in files_to_copy:
        shutil.copy2(str(DATA_DIR / src), os.path.join(repo_dir, dst))

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add BoomAI automated code review\n\n"
         "- AI-powered review using Gemini 3.1 Pro\n"
         "- Multi-language: TypeScript, JavaScript, Python, C#, Java, Go\n"
         "- All env vars prefixed BOOMAI_* (no conflicts)"],
        cwd=repo_dir, check=True, capture_output=True,
    )

    # 4. Push + create PR + merge
    print("  [4/5] Pushing and merging...")
    subprocess.run(
        ["git", "push", "-u", "origin", "boomai/install"],
        cwd=repo_dir, check=True, capture_output=True,
    )

    import re
    pr_url = gh("pr", "create", "--repo", repo,
                "--base", branch, "--head", "boomai/install",
                "--title", "Add BoomAI automated code review",
                "--body", "Automated BoomAI setup. Auto-merging.")

    pr_num = re.search(r'(\d+)$', pr_url).group(1)
    gh("pr", "merge", pr_num, "--repo", repo, "--merge", "--delete-branch")

    # 5. Cleanup
    print("  [5/5] Cleaning up...")
    shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n  BoomAI installed on {repo}!")
    print(f"  Every PR -> {branch} will be auto-reviewed.\n")


# ============================================================
#  Command: review / apply-all / apply
# ============================================================

async def run_local_review(base: str, head: str, repo_path: str = ".",
                           exclude: list[str] | None = None) -> ReviewSummary:
    print(f"\n  Comparing {head} -> {base}")
    if repo_path != ".":
        print(f"  Repo: {repo_path}")
    if exclude:
        print(f"  Excluding: {', '.join(exclude)}")

    all_files = git_changed_files(base, head, repo_path, exclude=exclude)
    reviewable = filter_reviewable_files(all_files)
    languages = detect_languages(all_files)
    print(f"  {len(all_files)} files changed, {len(reviewable)} reviewable")
    print(f"  Languages: {', '.join(languages)}")

    print(f"  Running static analysis...")
    try:
        findings = run_semgrep(reviewable, languages) if reviewable else []
        print(f"  Semgrep: {len(findings)} finding(s)")
    except Exception:
        findings = []
        print(f"  Semgrep skipped (not available locally)")

    diff = git_diff(base, head, repo_path, exclude=exclude) or ""
    print(f"  Diff size: {len(diff)} chars")

    print(f"  Sending to Gemini ({settings.llm_model})...")
    return await review_with_gemini(
        diff=diff,
        findings=findings,
        changed_files=[{"filename": f} for f in reviewable],
        detected_languages=languages,
    )


def _build_excludes(args) -> list[str] | None:
    excludes = []
    if not getattr(args, "no_auto_exclude", False):
        excludes.extend(BOOMAI_EXCLUDES)
    if getattr(args, "exclude", None):
        excludes.extend(args.exclude)
    return excludes or None


def cmd_review(args):
    require_api_key()
    repo_path = os.path.abspath(args.path)
    exclude = _build_excludes(args)
    review = asyncio.run(run_local_review(args.base, args.head, repo_path, exclude=exclude))
    print_review(review)


def cmd_apply_all(args):
    require_api_key()
    repo_path = os.path.abspath(args.path)
    exclude = _build_excludes(args)
    review = asyncio.run(run_local_review(args.base, args.head, repo_path, exclude=exclude))
    print_review(review)

    print(f"  Applying all fixes locally (no commit)...")
    count = apply_local(review.findings, repo_path)
    print(f"\n  Done! {count} fix(es) applied. Run `git diff` to see changes.")


def cmd_apply(args):
    require_api_key()
    repo_path = os.path.abspath(args.path)
    exclude = _build_excludes(args)
    review = asyncio.run(run_local_review(args.base, args.head, repo_path, exclude=exclude))
    print_review(review)

    file_filter = getattr(args, "file", None)
    label = f"fixes for {file_filter}" if file_filter else "all fixes"
    print(f"  Applying {label} locally (no commit)...")
    count = apply_local(review.findings, repo_path, file_filter)
    print(f"\n  Done! {count} fix(es) applied. Run `git diff` to see changes.")


# ============================================================
#  Command: scan
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
                         include: list[str] | None = None) -> ReviewSummary:
    """Scan entire codebase and return AI review."""
    from boomai.gemini_review import scan_with_gemini

    print(f"\n  Scanning codebase...")
    if repo_path != ".":
        print(f"  Repo: {repo_path}")
    if include:
        print(f"  Include: {', '.join(include)}")
    if exclude:
        print(f"  Excluding: {', '.join(exclude)}")

    all_files = collect_files(repo_path, exclude=exclude, include=include)
    reviewable = filter_reviewable_files(all_files)
    languages = detect_languages(all_files)

    print(f"  {len(all_files)} files found, {len(reviewable)} reviewable")
    print(f"  Languages: {', '.join(languages) if languages else 'none detected'}")

    if not reviewable:
        return ReviewSummary(
            summary="No reviewable source files found.",
            findings=[],
            critical_count=0,
            has_critical=False,
        )

    if len(reviewable) > settings.scan_max_files:
        print(f"  Warning: {len(reviewable)} files exceeds limit of {settings.scan_max_files}.")
        print(f"  Use --include to narrow scope.")
        return ReviewSummary(
            summary=f"Scan aborted: {len(reviewable)} files exceeds limit of {settings.scan_max_files}.",
            findings=[],
            critical_count=0,
            has_critical=False,
        )

    # Run Semgrep on ALL reviewable files (in batches)
    print(f"  Running static analysis...")
    SEMGREP_BATCH = 50
    findings = []
    try:
        for i in range(0, len(reviewable), SEMGREP_BATCH):
            batch = reviewable[i:i + SEMGREP_BATCH]
            findings.extend(run_semgrep(batch, languages))
        print(f"  Semgrep: {len(findings)} finding(s)")
    except Exception:
        print(f"  Semgrep skipped (not available locally)")

    # Read file contents
    file_contents = read_file_contents(reviewable, repo_path)
    total_chars = sum(len(c) for _, c in file_contents)
    num_chunks = max(1, -(-total_chars // settings.max_scan_chars))
    print(f"  Total source: {total_chars:,} chars across {len(file_contents)} files")
    print(f"  Sending to Gemini ({settings.llm_model})"
          f"{f' in {num_chunks} chunk(s)' if num_chunks > 1 else ''}...")

    return await scan_with_gemini(
        file_contents=file_contents,
        findings=findings,
        detected_languages=languages,
    )


def cmd_scan(args):
    """Scan entire codebase for issues."""
    require_api_key()
    repo_path = os.path.abspath(args.path)
    exclude = list(BOOMAI_EXCLUDES)
    if getattr(args, "exclude", None):
        exclude.extend(args.exclude)
    include = getattr(args, "include", None)

    review = asyncio.run(run_local_scan(repo_path, exclude=exclude, include=include))
    print_review(review)

    if getattr(args, "apply", False) and review.findings:
        print(f"  Applying all fixes locally (no commit)...")
        count = apply_local(review.findings, repo_path)
        print(f"\n  Done! {count} fix(es) applied. Run `git diff` to see changes.")


# ============================================================
#  Command: pr list / pr apply
# ============================================================

def get_gh_token() -> str:
    token = settings.github_token
    # Skip placeholder values from .env
    if not token or token.startswith("your-"):
        token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        try:
            token = gh("auth", "token")
        except Exception:
            pass
    return token


def cmd_pr_list(args):
    from boomai.apply_fixes import FixApplier

    settings.github_repository = args.repo
    settings.pr_number = args.pr
    settings.github_token = get_gh_token()
    if not settings.github_token:
        print("  Error: No GitHub token. Run `gh auth login` or set BOOMAI_GITHUB_TOKEN.")
        return

    applier = FixApplier()
    suggestions = applier.get_pending_suggestions()

    if not suggestions:
        print(f"\n  No pending suggestions on PR #{args.pr}.\n")
        return

    print(f"\n  {len(suggestions)} pending suggestion(s) on PR #{args.pr}:\n")
    for i, s in enumerate(suggestions, 1):
        preview = s["suggestion"][:80].replace("\n", " ")
        print(f"  #{i} {s['file']}:{s['line']}")
        print(f"      {preview}...")
    print()


def cmd_pr_apply(args):
    from boomai.apply_fixes import FixApplier

    settings.github_repository = args.repo
    settings.pr_number = args.pr
    settings.github_token = get_gh_token()
    if not settings.github_token:
        print("  Error: No GitHub token. Run `gh auth login` or set BOOMAI_GITHUB_TOKEN.")
        return

    applier = FixApplier()
    file_filter = getattr(args, "file", None)

    if file_filter:
        print(f"\n  Applying suggestions for {file_filter} on PR #{args.pr}...")
        applier.apply_file(file_filter)
    else:
        print(f"\n  Applying all suggestions on PR #{args.pr}...")
        applier.apply_all()


# ============================================================
#  API key management
# ============================================================

GLOBAL_ENV_DIR = Path.home() / ".boomai"
GLOBAL_ENV_FILE = GLOBAL_ENV_DIR / ".env"


def _save_api_key(key: str):
    """Save API key to ~/.boomai/.env"""
    GLOBAL_ENV_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    if GLOBAL_ENV_FILE.exists():
        lines = GLOBAL_ENV_FILE.read_text(encoding="utf-8").splitlines()
    # Replace or add the key
    new_lines = [l for l in lines if not l.startswith("BOOMAI_GOOGLE_API_KEY=")]
    new_lines.append(f"BOOMAI_GOOGLE_API_KEY={key}")
    GLOBAL_ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def require_api_key():
    if settings.google_api_key:
        return
    # No key found — prompt user
    print("\n  No API key found!")
    print("  Get one at: https://aistudio.google.com/apikey\n")
    key = input("  Enter your Gemini API key: ").strip()
    if not key:
        print("  Error: No key provided.")
        sys.exit(1)
    _save_api_key(key)
    settings.google_api_key = key
    print(f"  Key saved to {GLOBAL_ENV_FILE}")
    print()


def cmd_config(args):
    """View or update BoomAI configuration."""
    if args.config_command == "set-key":
        key = input("  Enter your new Gemini API key: ").strip()
        if not key:
            print("  Error: No key provided.")
            return
        _save_api_key(key)
        settings.google_api_key = key
        print(f"  Key saved to {GLOBAL_ENV_FILE}")
    elif args.config_command == "show":
        key = settings.google_api_key
        if key:
            masked = key[:8] + "..." + key[-4:] if len(key) > 12 else "***"
            print(f"\n  API Key: {masked}")
        else:
            print("\n  API Key: not set")
        print(f"  Model: {settings.llm_model}")
        print(f"  Config: {GLOBAL_ENV_FILE}")
        print()
    else:
        print("  Usage: boom-ai config [show|set-key]")


# ============================================================
#  Main CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        prog="boom-ai",
        description="BoomAI — AI-powered code review tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples (run from inside your project):
  boom-ai scan                    # full codebase scan
  boom-ai scan --apply            # scan + auto-apply fixes
  boom-ai scan --include src/     # scan only src/
  boom-ai review analytics development
  boom-ai apply-all analytics development
  boom-ai setup dgodolias/QuaR --branch development
""",
    )
    sub = parser.add_subparsers(dest="command")

    # --- setup ---
    p_setup = sub.add_parser("setup", help="Install BoomAI on a GitHub repo")
    p_setup.add_argument("repo", help="owner/repo")
    p_setup.add_argument("--branch", default="main", help="Base branch (default: main)")

    # --- review ---
    p_review = sub.add_parser("review", help="Review code between branches")
    p_review.add_argument("head", help="Branch being reviewed")
    p_review.add_argument("base", help="Merge target branch")
    p_review.add_argument("--path", default=".", help="Path to repo (default: current dir)")
    p_review.add_argument("--exclude", nargs="+", help="Exclude paths")
    p_review.add_argument("--no-auto-exclude", action="store_true",
                          help="Don't auto-exclude BoomAI files")

    # --- apply-all ---
    p_apply_all = sub.add_parser("apply-all", help="Review + apply ALL fixes locally (no commit)")
    p_apply_all.add_argument("head", help="Branch being reviewed")
    p_apply_all.add_argument("base", help="Merge target branch")
    p_apply_all.add_argument("--path", default=".", help="Path to repo (default: current dir)")
    p_apply_all.add_argument("--exclude", nargs="+", help="Exclude paths")
    p_apply_all.add_argument("--no-auto-exclude", action="store_true",
                             help="Don't auto-exclude BoomAI files")

    # --- apply ---
    p_apply = sub.add_parser("apply", help="Review + apply fixes for specific file")
    p_apply.add_argument("head", help="Branch being reviewed")
    p_apply.add_argument("base", help="Merge target branch")
    p_apply.add_argument("--file", help="Apply fixes only for this file")
    p_apply.add_argument("--path", default=".", help="Path to repo (default: current dir)")
    p_apply.add_argument("--exclude", nargs="+", help="Exclude paths")
    p_apply.add_argument("--no-auto-exclude", action="store_true",
                          help="Don't auto-exclude BoomAI files")

    # --- scan ---
    p_scan = sub.add_parser("scan", help="Scan entire codebase for issues (no branches needed)")
    p_scan.add_argument("--path", default=".", help="Path to repo (default: current dir)")
    p_scan.add_argument("--exclude", nargs="+", help="Exclude paths")
    p_scan.add_argument("--include", nargs="+", help="Only scan these paths (e.g., src/ lib/)")
    p_scan.add_argument("--apply", action="store_true",
                        help="Automatically apply all fixable suggestions")

    # --- config ---
    p_config = sub.add_parser("config", help="View or update configuration")
    config_sub = p_config.add_subparsers(dest="config_command")
    config_sub.add_parser("show", help="Show current config")
    config_sub.add_parser("set-key", help="Set a new API key")

    # --- pr ---
    p_pr = sub.add_parser("pr", help="Work with GitHub PR suggestions")
    pr_sub = p_pr.add_subparsers(dest="pr_command")

    p_pr_list = pr_sub.add_parser("list", help="List pending suggestions")
    p_pr_list.add_argument("repo", help="owner/repo")
    p_pr_list.add_argument("pr", type=int, help="PR number")

    p_pr_apply = pr_sub.add_parser("apply", help="Apply suggestions (commits to GitHub)")
    p_pr_apply.add_argument("repo", help="owner/repo")
    p_pr_apply.add_argument("pr", type=int, help="PR number")
    p_pr_apply.add_argument("--file", help="Apply only for this file")

    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    if args.command == "setup":
        cmd_setup(args)
    elif args.command == "review":
        cmd_review(args)
    elif args.command == "apply-all":
        cmd_apply_all(args)
    elif args.command == "apply":
        cmd_apply(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "config":
        cmd_config(args)
    elif args.command == "pr":
        if args.pr_command == "list":
            cmd_pr_list(args)
        elif args.pr_command == "apply":
            cmd_pr_apply(args)
        else:
            p_pr.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
