import logging
from github import Github
from boomai.config import settings
from boomai.models import ReviewSummary

logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self):
        self.gh = Github(settings.github_token)
        self.repo = self.gh.get_repo(settings.github_repository)
        self.pr = self.repo.get_pull(settings.pr_number)

    def get_changed_files(self) -> list[dict]:
        """Return list of changed files with their patches."""
        files = []
        for f in self.pr.get_files():
            files.append({
                "filename": f.filename,
                "patch": f.patch or "",
                "status": f.status,
                "additions": f.additions,
                "deletions": f.deletions,
            })
        return files

    def get_diff(self) -> str:
        """Get full PR diff text from file patches."""
        files = self.get_changed_files()
        diff_parts = []
        for f in files:
            diff_parts.append(
                f"--- a/{f['filename']}\n+++ b/{f['filename']}\n{f['patch']}"
            )
        return "\n".join(diff_parts)

    def post_review(self, summary: ReviewSummary):
        """Post a PR review with inline comments and suggestion blocks."""
        commits = list(self.pr.get_commits())
        commit = commits[-1]

        comments = []
        for finding in summary.findings:
            body = finding.body
            if finding.suggestion:
                body += f"\n\n```suggestion\n{finding.suggestion}\n```"

            comment_kwargs = {
                "body": body,
                "path": finding.file,
                "line": finding.line,
            }
            if finding.end_line and finding.end_line != finding.line:
                comment_kwargs["start_line"] = finding.line
                comment_kwargs["line"] = finding.end_line

            comments.append(comment_kwargs)

        review_body = f"## BoomAI Review\n\n{summary.summary}"
        if summary.has_critical:
            review_body += f"\n\n**{summary.critical_count} critical issue(s) found.**"

        try:
            self.pr.create_review(
                commit=commit,
                body=review_body,
                event="COMMENT",
                comments=comments,
            )
            logger.info(f"Posted review with {len(comments)} inline comment(s)")
        except Exception as e:
            # Fallback: post as simple comment if inline review fails
            logger.warning(f"Inline review failed ({e}), posting as comment")
            comment_body = review_body + "\n\n---\n\n"
            for finding in summary.findings:
                comment_body += (
                    f"**{finding.file}:{finding.line}**\n"
                    f"{finding.body}\n\n"
                )
            self.pr.create_issue_comment(comment_body)

    def post_comment(self, body: str):
        """Post a standalone issue comment on the PR."""
        self.pr.create_issue_comment(body)
