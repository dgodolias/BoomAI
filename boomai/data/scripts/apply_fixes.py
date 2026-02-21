"""Handle /boomAI commands for applying suggested fixes."""

import logging
import re

from github import Github

from scripts.config import settings

logger = logging.getLogger(__name__)


class FixApplier:
    def __init__(self):
        self.gh = Github(settings.github_token)
        self.repo = self.gh.get_repo(settings.github_repository)
        self.pr = self.repo.get_pull(settings.pr_number)

    def get_pending_suggestions(self) -> list[dict]:
        """Extract all suggestion blocks from BoomAI review comments."""
        suggestions = []
        for comment in self.pr.get_review_comments():
            if "```suggestion" not in comment.body:
                continue
            matches = re.findall(
                r"```suggestion\n(.*?)```",
                comment.body,
                re.DOTALL,
            )
            for match in matches:
                suggestions.append({
                    "file": comment.path,
                    "line": comment.line,
                    "suggestion": match.strip(),
                    "comment_id": comment.id,
                })
        return suggestions

    def apply_all(self):
        """Apply all pending suggestions."""
        suggestions = self.get_pending_suggestions()
        if not suggestions:
            self.pr.create_issue_comment(
                "**BoomAI:** No pending suggestions to apply."
            )
            return
        self._apply_suggestions(suggestions)
        self.pr.create_issue_comment(
            f"**BoomAI:** Applied {len(suggestions)} suggestion(s)."
        )

    def apply_file(self, filename: str):
        """Apply all suggestions for a specific file."""
        suggestions = self.get_pending_suggestions()
        file_suggestions = [s for s in suggestions if s["file"] == filename]
        if not file_suggestions:
            self.pr.create_issue_comment(
                f"**BoomAI:** No pending suggestions for `{filename}`."
            )
            return
        self._apply_suggestions(file_suggestions)
        self.pr.create_issue_comment(
            f"**BoomAI:** Applied {len(file_suggestions)} suggestion(s) "
            f"for `{filename}`."
        )

    def apply_batch(self, comment_body: str):
        """Apply selected suggestions (checkboxes in comments)."""
        checked = re.findall(
            r"\[x\]\s*(?:Fix\s*)?#?(\d+)", comment_body, re.IGNORECASE
        )
        if not checked:
            self.pr.create_issue_comment(
                "**BoomAI:** No fixes selected. "
                "Use checkboxes like `- [x] Fix #1`."
            )
            return

        all_suggestions = self.get_pending_suggestions()
        selected = [
            all_suggestions[int(i) - 1]
            for i in checked
            if int(i) - 1 < len(all_suggestions)
        ]
        if not selected:
            self.pr.create_issue_comment(
                "**BoomAI:** Selected fix numbers are out of range."
            )
            return

        self._apply_suggestions(selected)
        self.pr.create_issue_comment(
            f"**BoomAI:** Applied {len(selected)} selected suggestion(s)."
        )

    def _apply_suggestions(self, suggestions: list[dict]):
        """Apply suggestions by creating commits on the PR branch."""
        by_file: dict[str, list[dict]] = {}
        for s in suggestions:
            by_file.setdefault(s["file"], []).append(s)

        branch = self.pr.head.ref
        for filepath, file_suggestions in by_file.items():
            try:
                contents = self.repo.get_contents(filepath, ref=branch)
                lines = contents.decoded_content.decode("utf-8").splitlines(True)

                # Apply in reverse line order to preserve line numbers
                for s in sorted(
                    file_suggestions, key=lambda x: x["line"], reverse=True
                ):
                    line_idx = s["line"] - 1
                    if 0 <= line_idx < len(lines):
                        lines[line_idx] = s["suggestion"] + "\n"

                new_content = "".join(lines)
                self.repo.update_file(
                    filepath,
                    f"BoomAI: Apply suggested fixes to {filepath}",
                    new_content,
                    contents.sha,
                    branch=branch,
                )
                logger.info(
                    f"Applied {len(file_suggestions)} fix(es) to {filepath}"
                )
            except Exception as e:
                logger.error(f"Failed to apply fixes to {filepath}: {e}")
                self.pr.create_issue_comment(
                    f"**BoomAI:** Failed to apply fixes to `{filepath}`: {e}"
                )


def handle_command(comment_body: str):
    """Parse and execute a /boomAI command."""
    body = comment_body.strip()

    if body.startswith("/boomAI apply-all"):
        FixApplier().apply_all()
    elif body.startswith("/boomAI apply-file"):
        filename = body.replace("/boomAI apply-file", "").strip()
        if filename:
            FixApplier().apply_file(filename)
        else:
            applier = FixApplier()
            applier.pr.create_issue_comment(
                "**BoomAI:** Usage: `/boomAI apply-file <filename>`"
            )
    elif body.startswith("/boomAI apply-batch"):
        FixApplier().apply_batch(body)
    else:
        logger.info(f"Unknown /boomAI command: {body[:100]}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BoomAI Fix Applier CLI")
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    parser.add_argument("--apply-all", action="store_true", help="Apply all suggestions")
    parser.add_argument("--apply-file", type=str, help="Apply suggestions for one file")
    parser.add_argument("--list", action="store_true", dest="list_suggestions", help="List pending suggestions")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings.github_repository = args.repo
    settings.pr_number = args.pr
    if not settings.github_token:
        import os
        token = os.environ.get("BOOMAI_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        settings.github_token = token

    if not settings.github_token:
        print("Error: Set BOOMAI_GITHUB_TOKEN in .env or as env var")
        raise SystemExit(1)

    applier = FixApplier()

    if args.list_suggestions:
        suggestions = applier.get_pending_suggestions()
        if not suggestions:
            print("No pending suggestions.")
        else:
            print(f"\n{len(suggestions)} pending suggestion(s):\n")
            for i, s in enumerate(suggestions, 1):
                print(f"  #{i} {s['file']}:{s['line']}")
                preview = s['suggestion'][:80].replace('\n', ' ')
                print(f"      {preview}...")
            print()
    elif args.apply_all:
        applier.apply_all()
    elif args.apply_file:
        applier.apply_file(args.apply_file)
    else:
        parser.print_help()
