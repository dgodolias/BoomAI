"""
BoomAI Unified CLI

Usage:
  boom-ai setup <owner/repo> [--branch development]
  boom-ai review <base> <head> [--path /repo]
  boom-ai apply <base> <head> [--path /repo] [--file path]
  boom-ai pr list <owner/repo> <pr-number>
  boom-ai pr apply <owner/repo> <pr-number> [--file path]
"""

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile

from scripts.config import settings
from scripts.gemini_review import review_with_gemini
from scripts.languages import detect_languages, filter_reviewable_files
from scripts.models import ReviewSummary
from scripts.static_analysis import run_semgrep

logger = logging.getLogger(__name__)
BOOMAI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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

def apply_local(findings: list, repo_path: str = ".", file_filter: str | None = None) -> int:
    """Apply suggestion fixes to local files (no commit)."""
    by_file: dict[str, list] = {}
    for f in findings:
        if not f.suggestion:
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
            lines = fh.readlines()

        for f in sorted(file_findings, key=lambda x: x.line, reverse=True):
            line_idx = f.line - 1
            if 0 <= line_idx < len(lines):
                lines[line_idx] = f.suggestion + "\n"
                applied += 1

        with open(full_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        print(f"  Applied {len(file_findings)} fix(es) to {filepath}")

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
        print("  Error: Set BOOMAI_GOOGLE_API_KEY in .env")
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

    # 3. Create branch + copy files
    print("  [3/5] Copying BoomAI files...")
    subprocess.run(["git", "checkout", branch], cwd=repo_dir,
                    check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "boomai/install"], cwd=repo_dir,
                    check=True, capture_output=True)

    # Copy BoomAI files
    for d in [".github/workflows", "scripts", "rules/semgrep"]:
        os.makedirs(os.path.join(repo_dir, d), exist_ok=True)

    files_to_copy = [
        (".github/workflows/boomai.yml", ".github/workflows/boomai.yml"),
        ("scripts/__init__.py", "scripts/__init__.py"),
        ("scripts/config.py", "scripts/config.py"),
        ("scripts/models.py", "scripts/models.py"),
        ("scripts/prompts.py", "scripts/prompts.py"),
        ("scripts/languages.py", "scripts/languages.py"),
        ("scripts/gemini_review.py", "scripts/gemini_review.py"),
        ("scripts/github_client.py", "scripts/github_client.py"),
        ("scripts/static_analysis.py", "scripts/static_analysis.py"),
        ("scripts/slack_notifier.py", "scripts/slack_notifier.py"),
        ("scripts/main.py", "scripts/main.py"),
        ("scripts/apply_fixes.py", "scripts/apply_fixes.py"),
        ("rules/semgrep/unity-rules.yml", "rules/semgrep/unity-rules.yml"),
        ("requirements.txt", "requirements.txt"),
    ]
    for src, dst in files_to_copy:
        shutil.copy2(os.path.join(BOOMAI_DIR, src), os.path.join(repo_dir, dst))

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

    pr_url = gh("pr", "create", "--repo", repo,
                "--base", branch, "--head", "boomai/install",
                "--title", "Add BoomAI automated code review",
                "--body", "Automated BoomAI setup. Auto-merging.")

    import re
    pr_num = re.search(r'(\d+)$', pr_url).group(1)
    gh("pr", "merge", pr_num, "--repo", repo, "--merge", "--delete-branch")

    # 5. Cleanup
    print("  [5/5] Cleaning up...")
    shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n  BoomAI installed on {repo}!")
    print(f"  Every PR -> {branch} will be auto-reviewed.\n")


# ============================================================
#  Command: review / apply
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
    from scripts.apply_fixes import FixApplier

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
    from scripts.apply_fixes import FixApplier

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
#  Helpers
# ============================================================

def require_api_key():
    if not settings.google_api_key:
        print("  Error: Set BOOMAI_GOOGLE_API_KEY in .env or as env var")
        sys.exit(1)


# ============================================================
#  Main CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        prog="boom-ai",
        description="BoomAI — AI-powered code review tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  setup              Install BoomAI on a GitHub repo
  review             Review code between two branches (local)
  apply              Review + apply fixes locally (no commit)
  pr list            List pending suggestions on a PR
  pr apply           Apply suggestions on a PR (commits to GitHub)

Examples:
  boom-ai setup dgodolias/QuaR --branch development
  boom-ai review development feature-branch --path /repo
  boom-ai apply development feature-branch --path /repo
  boom-ai apply development feature-branch --file src/app.ts
  boom-ai pr list dgodolias/QuaR 3
  boom-ai pr apply dgodolias/QuaR 3
  boom-ai pr apply dgodolias/QuaR 3 --file src/app.ts
""",
    )
    sub = parser.add_subparsers(dest="command")

    # --- setup ---
    p_setup = sub.add_parser("setup", help="Install BoomAI on a GitHub repo")
    p_setup.add_argument("repo", help="owner/repo")
    p_setup.add_argument("--branch", default="main", help="Base branch (default: main)")

    # --- review ---
    p_review = sub.add_parser("review", help="Review code between branches (local)")
    p_review.add_argument("base", help="Base branch")
    p_review.add_argument("head", help="Head branch")
    p_review.add_argument("--path", default=".", help="Path to git repo")
    p_review.add_argument("--exclude", nargs="+", help="Exclude file paths (glob patterns)")
    p_review.add_argument("--no-auto-exclude", action="store_true",
                          help="Don't auto-exclude BoomAI files")

    # --- apply ---
    p_apply = sub.add_parser("apply", help="Review + apply fixes locally (no commit)")
    p_apply.add_argument("base", help="Base branch")
    p_apply.add_argument("head", help="Head branch")
    p_apply.add_argument("--path", default=".", help="Path to git repo")
    p_apply.add_argument("--file", help="Apply fixes only for this file")
    p_apply.add_argument("--exclude", nargs="+", help="Exclude file paths (glob patterns)")
    p_apply.add_argument("--no-auto-exclude", action="store_true",
                          help="Don't auto-exclude BoomAI files")

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
    elif args.command == "apply":
        cmd_apply(args)
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
