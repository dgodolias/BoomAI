from __future__ import annotations

from ...app.orchestrator import StaticAnalysisResult, run_static_analysis_suite


class StaticAnalysisService:
    """High-level static analysis orchestration service."""

    def run(
        self,
        *,
        repo_path: str,
        reviewable_files: list[str],
        detected_languages: list[str],
        on_progress=None,
    ) -> StaticAnalysisResult:
        return run_static_analysis_suite(
            repo_path=repo_path,
            reviewable_files=reviewable_files,
            detected_languages=detected_languages,
            on_progress=on_progress,
        )
