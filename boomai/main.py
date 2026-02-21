"""BoomAI - Main orchestrator for AI-powered code review."""

import asyncio
import json
import logging
import os
import sys

from boomai.config import settings
from boomai.github_client import GitHubClient
from boomai.languages import detect_languages, filter_reviewable_files
from boomai.static_analysis import (
    run_semgrep,
    filter_to_changed_files,
    prioritize_findings,
)
from boomai.gemini_review import review_with_gemini
from boomai.slack_notifier import send_slack_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("boomai")


async def run_review():
    """Main review pipeline."""
    logger.info("BoomAI review starting...")

    # 1. Initialize GitHub client
    gh = GitHubClient()

    # 2. Get changed files and detect languages
    changed_files = gh.get_changed_files()
    filenames = [f["filename"] for f in changed_files]

    detected_languages = detect_languages(filenames)
    reviewable_files = filter_reviewable_files(filenames)

    logger.info(
        f"PR #{settings.pr_number}: {len(changed_files)} files changed, "
        f"{len(reviewable_files)} reviewable, "
        f"languages: {detected_languages or ['none']}"
    )

    if not reviewable_files:
        gh.post_comment(
            "**BoomAI:** No supported source files changed. Skipping review."
        )
        logger.info("No reviewable files, exiting")
        return

    # 3. Run static analysis (language-aware)
    logger.info("Running static analysis...")
    semgrep_findings = run_semgrep(reviewable_files, detected_languages)
    semgrep_findings = filter_to_changed_files(semgrep_findings, filenames)
    top_findings = prioritize_findings(semgrep_findings, settings.max_findings)

    logger.info(
        f"Static analysis: {len(semgrep_findings)} total, "
        f"{len(top_findings)} selected for AI review"
    )

    # 4. Get PR diff and run AI review (language-aware)
    diff = gh.get_diff()
    logger.info(f"Diff size: {len(diff)} chars")

    review = await review_with_gemini(
        diff, top_findings, changed_files, detected_languages
    )
    logger.info(
        f"AI review: {len(review.findings)} findings, "
        f"{review.critical_count} critical"
    )

    # 5. Post review to GitHub
    if review.findings:
        gh.post_review(review)
    else:
        gh.post_comment(f"**BoomAI Review**\n\n{review.summary}")

    # 6. Send Slack alert if critical issues found
    pr_url = (
        f"https://github.com/{settings.github_repository}"
        f"/pull/{settings.pr_number}"
    )
    pr_title = gh.pr.title
    await send_slack_alert(review, pr_url, pr_title)

    logger.info("BoomAI review complete!")


def main():
    """Entry point — parse GitHub event and run the review pipeline."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        with open(event_path) as f:
            event = json.load(f)

        if "pull_request" in event:
            settings.pr_number = event["pull_request"]["number"]
        elif "issue" in event:
            settings.pr_number = event["issue"]["number"]

    if os.environ.get("PR_NUMBER"):
        settings.pr_number = int(os.environ["PR_NUMBER"])

    settings.github_repository = os.environ.get(
        "GITHUB_REPOSITORY", settings.github_repository
    )
    settings.github_token = os.environ.get(
        "GITHUB_TOKEN", settings.github_token
    )

    if not settings.pr_number:
        logger.error(
            "No PR number found. Set PR_NUMBER env var or run from GitHub Actions."
        )
        sys.exit(1)

    if not settings.google_api_key:
        logger.error("GOOGLE_API_KEY not set.")
        sys.exit(1)

    asyncio.run(run_review())


if __name__ == "__main__":
    main()
