"""Cross-file context retrieval for BoomAI review prompts."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.models import IssueSeed
from .indexer import CodeIndex, SymbolDefinition


@dataclass(frozen=True)
class ContextSnippet:
    file: str
    reason: str
    content: str


@dataclass(frozen=True)
class RetrievalResult:
    issue_seeds: list[IssueSeed]
    snippets: list[ContextSnippet]


def _extract_snippet(content: str, line: int, radius: int = 18) -> str:
    lines = content.splitlines()
    if not lines:
        return ""
    start = max(0, line - 1 - radius)
    end = min(len(lines), line - 1 + radius + 1)
    return "\n".join(
        f"{idx + 1}: {lines[idx]}"
        for idx in range(start, end)
    )


def _score_definition(
    definition: SymbolDefinition,
    primary_file: str,
    primary_namespace: str | None,
    primary_usings: list[str],
) -> tuple[int, int]:
    score = 0
    if definition.file == primary_file:
        score -= 100
    if primary_namespace and definition.namespace == primary_namespace:
        score += 5
    if definition.namespace and definition.namespace in primary_usings:
        score += 3
    if definition.kind in {"class", "interface", "struct", "enum"}:
        score += 2
    return score, -definition.line


def retrieve_related_context(
    primary_files: list[str],
    repo_file_map: dict[str, str],
    code_index: CodeIndex | None,
    issue_seeds: list[IssueSeed] | None = None,
    max_issue_seeds: int = 12,
    max_snippets: int = 10,
    max_snippet_chars: int = 12000,
) -> RetrievalResult:
    """Retrieve static issue seeds and related cross-file snippets."""
    issue_seeds = issue_seeds or []
    primary_set = set(primary_files)

    selected_issue_seeds = [
        seed for seed in issue_seeds
        if seed.file in primary_set
    ]
    selected_issue_seeds = sorted(
        selected_issue_seeds,
        key=lambda s: (s.severity.value, s.file, s.line),
    )[:max_issue_seeds]

    if code_index is None:
        return RetrievalResult(issue_seeds=selected_issue_seeds, snippets=[])

    snippets: list[ContextSnippet] = []
    seen: set[tuple[str, int, str]] = set()
    remaining_chars = max_snippet_chars

    for primary_file in primary_files:
        identifiers = code_index.file_identifiers.get(primary_file, set())
        file_symbols = set(code_index.file_symbols.get(primary_file, []))
        namespace = code_index.file_namespaces.get(primary_file)
        usings = code_index.file_usings.get(primary_file, [])

        candidate_names = [name for name in identifiers if name in code_index.symbols_by_name]
        candidate_names.sort(key=lambda name: (name not in file_symbols, name))

        for name in candidate_names:
            definitions = code_index.symbols_by_name.get(name, [])
            ranked = sorted(
                definitions,
                key=lambda item: _score_definition(item, primary_file, namespace, usings),
                reverse=True,
            )
            for definition in ranked:
                if definition.file in primary_set:
                    continue
                key = (definition.file, definition.line, name)
                if key in seen:
                    continue
                related_content = repo_file_map.get(definition.file)
                if not related_content:
                    continue
                snippet_text = _extract_snippet(related_content, definition.line)
                if not snippet_text:
                    continue
                cost = len(snippet_text)
                if cost > remaining_chars:
                    break
                seen.add(key)
                snippets.append(ContextSnippet(
                    file=definition.file,
                    reason=f"Definition of `{name}` referenced from `{primary_file}`",
                    content=snippet_text,
                ))
                remaining_chars -= cost
                if len(snippets) >= max_snippets or remaining_chars <= 0:
                    return RetrievalResult(
                        issue_seeds=selected_issue_seeds,
                        snippets=snippets,
                    )
                break

    return RetrievalResult(issue_seeds=selected_issue_seeds, snippets=snippets)
