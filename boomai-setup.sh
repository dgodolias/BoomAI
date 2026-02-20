#!/usr/bin/env bash
#
# BoomAI Setup Script
# Usage: ./boomai-setup.sh owner/repo [base-branch]
#
# This script:
# 1. Sets the GOOGLE_API_KEY secret on the target repo
# 2. Creates a branch with all BoomAI files
# 3. Pushes and creates a PR to merge BoomAI into the repo
#
# Prerequisites:
# - gh CLI installed and authenticated (gh auth login)
# - GOOGLE_API_KEY env var set, or will read from .env file
#

set -euo pipefail

REPO="${1:-}"
BASE_BRANCH="${2:-main}"
BOOMAI_DIR="$(cd "$(dirname "$0")" && pwd)"
SETUP_BRANCH="boomai/setup"

if [ -z "$REPO" ]; then
    echo "Usage: $0 owner/repo [base-branch]"
    echo "Example: $0 dgodolias/QuaR development"
    exit 1
fi

echo "=== BoomAI Setup for $REPO ==="
echo ""

# --- Step 1: Set GOOGLE_API_KEY secret ---
if [ -z "${GOOGLE_API_KEY:-}" ]; then
    # Try reading from .env file
    if [ -f "$BOOMAI_DIR/.env" ]; then
        GOOGLE_API_KEY=$(grep '^GOOGLE_API_KEY=' "$BOOMAI_DIR/.env" | cut -d'=' -f2)
    fi
fi

if [ -z "${GOOGLE_API_KEY:-}" ]; then
    echo "Error: GOOGLE_API_KEY not found. Set it as env var or in .env file."
    exit 1
fi

echo "[1/4] Setting GOOGLE_API_KEY secret on $REPO..."
echo "$GOOGLE_API_KEY" | gh secret set GOOGLE_API_KEY -R "$REPO"
echo "  Done."

# --- Step 2: Clone and create branch ---
TMPDIR=$(mktemp -d)
echo "[2/4] Cloning $REPO into temp directory..."
gh repo clone "$REPO" "$TMPDIR/repo" -- --quiet
cd "$TMPDIR/repo"

git checkout "$BASE_BRANCH" 2>/dev/null || git checkout -b "$BASE_BRANCH"
git checkout -b "$SETUP_BRANCH"

# --- Step 3: Copy BoomAI files ---
echo "[3/4] Copying BoomAI files..."

# Create directories
mkdir -p .github/workflows
mkdir -p scripts
mkdir -p rules/semgrep

# Copy all BoomAI files
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
git commit -m "Add BoomAI automated code review system

- AI-powered code review using Gemini 3 Pro
- Multi-language support (TypeScript, JavaScript, Python, C#, Java, Go)
- Static analysis with Semgrep
- Inline PR comments with suggestion blocks
- /boomAI commands for applying fixes"

# --- Step 4: Push and create PR ---
echo "[4/4] Pushing and creating PR..."
git push -u origin "$SETUP_BRANCH"

gh pr create \
    --repo "$REPO" \
    --base "$BASE_BRANCH" \
    --head "$SETUP_BRANCH" \
    --title "Add BoomAI automated code review" \
    --body "## BoomAI Setup

Adds the BoomAI automated code review system to this repository.

### What it does
- Automatically reviews every PR using **Gemini 3 Pro AI**
- Runs **Semgrep** static analysis with language-appropriate rulesets
- Posts **inline review comments** with actionable suggestions
- Supports **suggestion blocks** (one-click apply in GitHub UI)

### Supported languages
TypeScript, JavaScript, Python, C#/Unity, Java, Go

### Commands
- \`/boomAI apply-all\` — Apply all suggested fixes
- \`/boomAI apply-file <filename>\` — Apply fixes for one file
- \`/boomAI apply-batch\` — Apply selected fixes (checkboxes)

### Files added
- \`.github/workflows/boomai.yml\` — GitHub Actions workflow
- \`scripts/\` — BoomAI Python scripts
- \`rules/semgrep/\` — Custom Semgrep rules
- \`requirements.txt\` — Python dependencies"

echo ""
echo "=== Setup complete! ==="
echo "PR created. Merge it to enable BoomAI on $REPO."
echo ""

# Cleanup
rm -rf "$TMPDIR"
