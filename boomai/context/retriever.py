"""Cross-file context retrieval for BoomAI review prompts."""

from __future__ import annotations

import re
from collections import Counter
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


@dataclass(frozen=True)
class _ScoredCandidate:
    score: int
    file: str
    line: int
    name: str
    reason: str
    content: str


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "will", "into", "while",
    "used", "when", "where", "have", "has", "had", "can", "could", "should", "would",
    "file", "files", "code", "data", "value", "values", "line", "lines", "null",
}


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
    *,
    query_tokens: set[str],
    primary_symbols: set[str],
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
    if definition.name in primary_symbols:
        score += 4
    if definition.name.lower() in query_tokens:
        score += 5
    if any(base.lower() in query_tokens for base in definition.bases):
        score += 2
    return score, -definition.line


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text) if token.lower() not in _STOPWORDS]


def _query_tokens(
    primary_file: str,
    identifiers: set[str],
    issue_seeds: list[IssueSeed],
) -> set[str]:
    tokens = set(_tokenize(primary_file))
    tokens.update(token.lower() for token in identifiers if len(token) >= 3)
    for seed in issue_seeds:
        tokens.update(_tokenize(seed.file))
        tokens.update(_tokenize(seed.rule_id))
        tokens.update(_tokenize(seed.message))
    return tokens


def _score_snippet_content(snippet_text: str, query_tokens: set[str]) -> int:
    if not snippet_text or not query_tokens:
        return 0
    token_counts = Counter(_tokenize(snippet_text))
    overlap = sum(1 for token in query_tokens if token in token_counts)
    hot_overlap = sum(min(2, token_counts[token]) for token in query_tokens if token_counts[token] > 0)
    return overlap * 3 + hot_overlap


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
        seed_tokens = _query_tokens(
            primary_file,
            identifiers,
            [seed for seed in selected_issue_seeds if seed.file == primary_file],
        )

        candidate_names = [name for name in identifiers if name in code_index.symbols_by_name]
        candidate_names.sort(key=lambda name: (name not in file_symbols, name))

        candidates: list[_ScoredCandidate] = []
        for name in candidate_names:
            definitions = code_index.symbols_by_name.get(name, [])
            ranked = sorted(
                definitions,
                key=lambda item: _score_definition(
                    item,
                    primary_file,
                    namespace,
                    usings,
                    query_tokens=seed_tokens,
                    primary_symbols=file_symbols,
                ),
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
                score_tuple = _score_definition(
                    definition,
                    primary_file,
                    namespace,
                    usings,
                    query_tokens=seed_tokens,
                    primary_symbols=file_symbols,
                )
                score = (
                    score_tuple[0] * 4
                    + _score_snippet_content(snippet_text, seed_tokens)
                    + (6 if definition.kind in {"class", "interface", "struct", "enum"} else 0)
                )
                candidates.append(
                    _ScoredCandidate(
                        score=score,
                        file=definition.file,
                        line=definition.line,
                        name=name,
                        reason=f"Definition of `{name}` referenced from `{primary_file}`",
                        content=snippet_text,
                    )
                )
                break

        for candidate in sorted(
            candidates,
            key=lambda item: (-item.score, item.file.lower(), item.line, item.name.lower()),
        ):
            key = (candidate.file, candidate.line, candidate.name)
            if key in seen:
                continue
            cost = len(candidate.content)
            if cost > remaining_chars:
                continue
            seen.add(key)
            snippets.append(
                ContextSnippet(
                    file=candidate.file,
                    reason=candidate.reason,
                    content=candidate.content,
                )
            )
            remaining_chars -= cost
            if len(snippets) >= max_snippets or remaining_chars <= 0:
                return RetrievalResult(
                    issue_seeds=selected_issue_seeds,
                    snippets=snippets,
                )

    return RetrievalResult(issue_seeds=selected_issue_seeds, snippets=snippets)
