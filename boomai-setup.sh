#!/usr/bin/env bash
#
# BoomAI Plug-and-Play Setup Script
# Usage: ./boomai-setup.sh owner/repo [base-branch]
#
# This script does EVERYTHING automatically:
# 1. Sets the BOOMAI_GOOGLE_API_KEY secret on the target repo
# 2. Creates a branch with all BoomAI files
# 3. Creates a PR, merges it, and cleans up
#
# After running, every PR targeting base-branch will be auto-reviewed.
#
# Prerequisites:
# - gh CLI installed and authenticated (gh auth login)
# - BOOMAI_GOOGLE_API_KEY env var set, or will read from .env file
#

set -euo pipefail

REPO="${1:-}"
BASE_BRANCH="${2:-main}"
BOOMAI_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_BRANCH="boomai/install"

if [ -z "$REPO" ]; then
    echo "Usage: $0 owner/repo [base-branch]"
    echo "Example: $0 dgodolias/QuaR development"
    exit 1
fi

echo "========================================"
echo "  BoomAI Plug-and-Play Setup"
echo "  Repo: $REPO"
echo "  Base: $BASE_BRANCH"
echo "========================================"
echo ""

# --- Step 1: Set BOOMAI_GOOGLE_API_KEY secret ---
if [ -z "${BOOMAI_GOOGLE_API_KEY:-}" ]; then
    # Try reading from .env file
    if [ -f "$BOOMAI_DIR/.env" ]; then
        BOOMAI_GOOGLE_API_KEY=$(grep '^BOOMAI_GOOGLE_API_KEY=' "$BOOMAI_DIR/.env" | cut -d'=' -f2)
    fi
fi

if [ -z "${BOOMAI_GOOGLE_API_KEY:-}" ]; then
    echo "Error: BOOMAI_GOOGLE_API_KEY not found."
    echo "Set it as env var or in .env file (BOOMAI_GOOGLE_API_KEY=your-key)"
    exit 1
fi

echo "[1/5] Setting BOOMAI_GOOGLE_API_KEY secret on $REPO..."
echo "$BOOMAI_GOOGLE_API_KEY" | gh secret set BOOMAI_GOOGLE_API_KEY -R "$REPO"
echo "  Done."

# --- Step 2: Clone and create branch ---
TMPDIR=$(mktemp -d)
echo "[2/5] Cloning $REPO..."
gh repo clone "$REPO" "$TMPDIR/repo" -- --quiet
cd "$TMPDIR/repo"

git checkout "$BASE_BRANCH" 2>/dev/null || git checkout -b "$BASE_BRANCH"

# Delete remote install branch if it exists from a previous run
git push origin --delete "$INSTALL_BRANCH" 2>/dev/null || true
git checkout -b "$INSTALL_BRANCH"

# --- Step 3: Copy BoomAI files ---
echo "[3/5] Copying BoomAI files..."

mkdir -p .github/workflows
mkdir -p scripts
mkdir -p rules/semgrep

cp "$BOOMAI_DIR/.github/workflows/boomai.yml" .github/workflows/
cp "$BOOMAI_DIR/scripts/__init__.py" scripts/
cp "$BOOMAI_DIR/scripts/config.py" scripts/
cp "$BOOMAI_DIR/scripts/models.py" scripts/
cp "$BOOMAI_DIR/scripts/prompts.py" scripts/
cp "$BOOMAI_DIR/scripts/languages.py" scripts/
cp "$BOOMAI_DIR/scripts/gemini_review.py" scripts/
cp "$BOOMAI_DIR/scripts/github_client.py" scripts/
cp "$BOOMAI_DIR/scripts/static_analysis.py" scripts/
cp "$BOOMAI_DIR/scripts/slack_notifier.py" scripts/
cp "$BOOMAI_DIR/scripts/main.py" scripts/
cp "$BOOMAI_DIR/scripts/apply_fixes.py" scripts/
cp "$BOOMAI_DIR/rules/semgrep/unity-rules.yml" rules/semgrep/
cp "$BOOMAI_DIR/requirements.txt" .

git add .
git commit -m "Add BoomAI automated code review

- AI-powered review using Gemini 3.1 Pro
- Multi-language: TypeScript, JavaScript, Python, C#, Java, Go
- Static analysis with Semgrep
- Inline PR suggestions with one-click apply
- All env vars prefixed BOOMAI_* (no conflicts)"

# --- Step 4: Push, create PR, and auto-merge ---
echo "[4/5] Pushing and creating PR..."
git push -u origin "$INSTALL_BRANCH"

PR_URL=$(gh pr create \
    --repo "$REPO" \
    --base "$BASE_BRANCH" \
    --head "$INSTALL_BRANCH" \
    --title "Add BoomAI automated code review" \
    --body "## BoomAI Setup (auto-merge)

Adds BoomAI to this repository. This PR will be auto-merged.

### What it does
- Automatically reviews every PR using **Gemini 3.1 Pro AI**
- Runs **Semgrep** static analysis
- Posts **inline suggestions** (one-click apply)
- All env vars prefixed \`BOOMAI_*\` (no conflicts with existing secrets)

### Supported languages
TypeScript, JavaScript, Python, C#/Unity, Java, Go

### Commands
- \`/boomAI apply-all\` — Apply all suggested fixes
- \`/boomAI apply-file <filename>\` — Apply fixes for one file")

echo "  PR created: $PR_URL"

# Extract PR number and merge
PR_NUM=$(echo "$PR_URL" | grep -o '[0-9]*$')
echo "  Merging PR #$PR_NUM..."
gh pr merge "$PR_NUM" --repo "$REPO" --merge --delete-branch

# --- Step 5: Cleanup ---
echo "[5/5] Cleaning up..."
cd /
rm -rf "$TMPDIR"

echo ""
echo "========================================"
echo "  BoomAI installed on $REPO!"
echo "  Every PR -> $BASE_BRANCH will be auto-reviewed."
echo "========================================"
