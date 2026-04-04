from __future__ import annotations

from ...core.models import IssueSeed
from ..indexer import CodeIndex
from ..retriever import RetrievalResult, retrieve_related_context


class RelatedContextRetriever:
    """Service wrapper around cross-file snippet retrieval."""

    def retrieve(
        self,
        *,
        primary_files: list[str],
        repo_file_map: dict[str, str],
        code_index: CodeIndex | None,
        issue_seeds: list[IssueSeed] | None = None,
        max_issue_seeds: int | None = None,
        max_snippets: int | None = None,
        max_snippet_chars: int | None = None,
    ) -> RetrievalResult:
        return retrieve_related_context(
            primary_files=primary_files,
            repo_file_map=repo_file_map,
            code_index=code_index,
            issue_seeds=issue_seeds,
            max_issue_seeds=max_issue_seeds,
            max_snippets=max_snippets,
            max_snippet_chars=max_snippet_chars,
        )
