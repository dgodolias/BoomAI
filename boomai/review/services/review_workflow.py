from __future__ import annotations

from ...analysis.languages import detect_languages, filter_reviewable_files
from ...core.config import settings
from ...core.models import ReviewSummary
from ...review.gemini_review import scan_with_gemini
from ...app.services.file_selection_service import (
    collect_files,
    read_file_contents,
    report_unreadable_files,
)


class ReviewWorkflow:
    """Owns review execution over file contents or repo files."""

    async def run_local_scan(
        self,
        *,
        repo_path: str = ".",
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
        if file_contents is None:
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

            lang_str = ", ".join(languages) if languages else "none detected"
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
                print("    Use --include to narrow scope.")
                return ReviewSummary(
                    summary=f"Scan aborted: {len(reviewable)} files exceeds limit of {settings.scan_max_files}.",
                    findings=[],
                    critical_count=0,
                    has_critical=False,
                )

            file_contents = read_file_contents(reviewable, repo_path)
            report_unreadable_files(reviewable, file_contents, debug=settings.scan_debug)
            languages = detect_languages([path for path, _ in file_contents])
        else:
            languages = detect_languages([path for path, _ in file_contents])

        total_chars = sum(len(content) for _, content in file_contents)
        scan_model = runtime_models.strong_model_id if runtime_models else settings.strong_model
        print(f"    {total_chars:,} chars across {len(file_contents)} files")
        print(f"    Model: {scan_model}")

        def progress(message: str) -> None:
            if on_progress:
                on_progress(message)
            else:
                print(f"  {message}")

        return await scan_with_gemini(
            file_contents=file_contents,
            detected_languages=languages,
            runtime_models=runtime_models,
            comments=comments,
            on_progress=progress,
            on_chunk_done=on_chunk_done,
            issue_seeds=issue_seeds,
            code_index=code_index,
        )
