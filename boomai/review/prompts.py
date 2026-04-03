"""Dynamic prompt builder for C#/Unity code review."""

from __future__ import annotations

from ..analysis.languages import LANGUAGES
from ..context.retriever import ContextSnippet
from ..core.models import IssueSeed
from .prompt_composer import append_fix_pack_guidance, append_scan_pack_guidance


def _build_language_extras(lang_configs: list) -> list[str]:
    """Build language-specific review focus bullets from each skill's prompt_extras."""
    extra = []
    for config in lang_configs:
        if config.prompt_extras:
            for bullet in config.prompt_extras:
                extra.append(f"   - {bullet}")
        else:
            extra.append(f"   - {config.name}-specific best practice violations")
    extra.append("   - Merge conflict artifacts and duplicate definitions")
    extra.append("   - Architecture/design concerns")
    return extra


_SECURITY_PATTERNS = [
    "",
    "## Security Patterns (check each explicitly)",
    "### Input Handling",
    "- Path traversal: is input decoded BEFORE validation? (`%2e%2e` -> `..` bypasses check)",
    "- Is MIME type from client trusted without server-side magic-byte check?",
    "- Are file paths normalized and verified to stay within a base directory?",
    "- Is there a whitelist regex instead of a blacklist for input validation?",
    "",
    "### Authentication & Authorization",
    "- Does auth return early for unknown user? (timing attack -> username enumeration)",
    "- Are there plaintext password comparisons or legacy plaintext fallbacks?",
    "- Is the same authz check applied consistently across ALL routes for a given resource?",
    "- Do auth hooks/middleware early-return after sending 401, or does the route handler still execute?",
    "- Mixed auth patterns in same codebase (session vs JWT vs localStorage)?",
    "",
    "### Async & Concurrency",
    "- Is synchronous I/O (File.ReadAllText, WebClient, HttpWebRequest) used inside async methods? Should use async equivalents (File.ReadAllTextAsync, HttpClient)",
    "- Does read-modify-write lack a transaction or lock? (race condition)",
    "- Is Task.WhenAll() used for bulk operations without per-task exception handling? (one failed task causes WhenAll to throw, silently dropping successful results)",
    "- Are there fire-and-forget async calls (no await, no .ContinueWith error handler)? Unhandled exceptions from fire-and-forget tasks are silently swallowed",
    "",
    "### API & Configuration",
    "- Does rate limiting have `skipOnError: true`? (errors bypass protection)",
    "- Is CORS configured with unvalidated env var origins?",
    "- Are list endpoints missing pagination? (memory exhaustion at scale)",
    "- Is error response format consistent across all routes? (`{ error }` vs `{ message }` vs `{ errors: [] }`)",
    "",
    "### Unity Game-Specific Patterns",
    "- ScriptableObject mutated at runtime without Instantiate -> shared state corruption across all references",
    "- Singleton with static MonoBehaviour reference but no duplicate guard on scene reload -> double managers, leaked events",
    "- Static event dictionary (SystemEventManager pattern) with StartListening but no StopListening -> delegates on destroyed objects throw MissingReferenceException, prevent GC",
    "- `Resources.Load` in loop or frequent path without caching result -> repeated disk access causes frame stutters",
    "- Event += subscription in Configure/Init without matching -= in OnDestroy/OnDisable -> memory leaks and stale callbacks after scene transitions",
    "- Coroutine started without storing reference or stopping on OnDisable/OnDestroy -> orphaned coroutines access destroyed objects",
]


def build_scan_response_schema() -> dict:
    """Return a JSON schema for findings-only scan responses."""
    finding_schema = {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "severity": {
                "type": "string",
                "enum": ["critical", "high", "medium", "low", "info"],
            },
            "message": {"type": "string"},
            "category": {
                "type": "string",
                "enum": [
                    "correctness",
                    "security",
                    "resource",
                    "performance",
                    "lifecycle",
                    "threading",
                    "bounds",
                    "data-integrity",
                    "api-contract",
                    "maintainability",
                    "other",
                ],
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
            "fixable": {"type": "boolean"},
            "patch_group_key": {"type": "string"},
        },
        "required": [
            "file",
            "line",
            "severity",
            "message",
            "category",
            "confidence",
            "fixable",
            "patch_group_key",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": finding_schema,
            },
            "critical_count": {"type": "integer"},
        },
        "required": ["summary", "findings", "critical_count"],
        "additionalProperties": False,
        "propertyOrdering": ["summary", "findings", "critical_count"],
    }


def build_fix_response_schema() -> dict:
    """Return a JSON schema for a single structured edit."""
    return {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding_indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                        "message": {"type": "string"},
                        "old_code": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                    "required": ["finding_indices", "file", "line", "old_code", "suggestion"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["edits"],
        "additionalProperties": False,
        "propertyOrdering": ["edits"],
    }


def build_plan_response_schema() -> dict:
    """Return a JSON schema for planning responses."""
    return {
        "type": "object",
        "properties": {
            "chunks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "dirs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "focus": {"type": "string"},
                    },
                    "required": ["dirs"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["chunks"],
        "additionalProperties": False,
        "propertyOrdering": ["chunks"],
    }


def build_scan_system_prompt(
    detected_languages: list[str],
    comments: bool = False,
    selected_pack_ids: list[str] | None = None,
) -> str:
    """Build system prompt for full-codebase scan mode."""
    lang_configs = [LANGUAGES[k] for k in detected_languages if k in LANGUAGES]
    lang_names = (
        ", ".join(c.name for c in lang_configs)
        if lang_configs
        else "general programming"
    )

    parts = [
        f"You are BoomAI, an expert code reviewer with deep expertise in {lang_names}.",
        "You are performing a FULL CODEBASE SCAN - reviewing entire source files, not a diff.",
        "",
        "## Your Role",
        "- Identify bugs, security vulnerabilities, and correctness issues",
        "- Find architectural problems and code smells",
        "- Spot performance anti-patterns and resource leaks",
        "- Check for missing error handling and edge cases",
        "- Validate static analysis findings (confirm real issues, flag false positives)",
        "- Use related cross-file context when it clarifies ownership, definitions, or call flow",
        "- Report actionable findings only; code fixes are generated in a separate pass",
        "",
        "## Focus Areas",
        "- Cross-file inconsistencies (naming, patterns, error handling)",
        "- Dead code and unused imports",
        "- Missing input validation and boundary checks",
        "- Concurrency and thread safety issues",
        "- Resource management (unclosed handles, missing cleanup)",
        "- Unity lifecycle ordering issues (Awake/Start/OnEnable dependencies, missing OnDestroy cleanup)",
        "- Hot path allocations (GC pressure from GetComponent, Physics, LINQ, string ops in Update/FixedUpdate)",
        "- Physics correctness (FixedUpdate vs Update, NonAlloc variants, raycast efficiency)",
        "- Merge conflict artifacts and duplicate function/class definitions",
    ]

    parts.extend(_SECURITY_PATTERNS)

    if lang_configs:
        parts.append("")
        parts.append("## Language-Specific Expertise")
        for config in lang_configs:
            if config.expertise:
                parts.append(f"### {config.name}")
                parts.append(f"- {config.expertise}")

    parts.extend([
        "",
        "## Output Format",
        "You MUST respond with valid JSON in this exact structure:",
        "{",
        '  "summary": "Brief overall assessment of the codebase (2-3 sentences)",',
        '  "findings": [',
        "    {",
        '      "file": "path/to/file.ext",',
        '      "line": 42,',
        '      "severity": "high",',
        '      "message": "Clear explanation of the issue and WHY it matters",',
        '      "category": "correctness",',
        '      "confidence": "high",',
        '      "fixable": true,',
        '      "patch_group_key": "bounds-guard-1"',
        "    }",
        "  ],",
        '  "critical_count": 0',
        "}",
        "",
        "## Rules",
        "- severity must be one of: critical, high, medium, low, info",
        "- category must be one of: correctness, security, resource, performance, lifecycle, threading, bounds, data-integrity, api-contract, maintainability, other",
        "- confidence must be high, medium, or low based on code evidence visible in the provided context",
        "- fixable must be true only when a small local code patch inside this repo can likely fix the issue safely",
        "- patch_group_key must group findings in the SAME file that should be fixed together in one local patch; otherwise use an empty string",
        "- line numbers are for reference only (to help locate the issue)",
        "",
        "## Finding Rules",
        "- Do NOT include code patches, old_code, suggestion, or diffs in this stage",
        "- Prefer bugs, correctness issues, leaks, lifecycle errors, and meaningful performance issues",
        "- Avoid noisy style-only findings unless they are truly high-value",
    ])

    parts.append(
        "- Keep each message compact and concrete; explain the risk in one sentence, not a paragraph"
    )

    parts.extend([
        "",
        "- Keep findings focused and actionable (max 30 per chunk)",
        "- Review EVERY file systematically - do not skip any. Distribute attention evenly across all files.",
        "- Prioritize high-severity issues over style nits",
        "- If the code looks good, say so in the summary with minimal/no findings",
        "- ALWAYS respond with valid JSON, nothing else",
    ])

    return append_scan_pack_guidance("\n".join(parts), selected_pack_ids)


def build_fix_system_prompt(
    comments: bool = False,
    selected_pack_ids: list[str] | None = None,
) -> str:
    """Build system prompt for grouped patch generation."""
    parts = [
        "You are BoomAI's patch generation module.",
        "You receive one target file, one local context window, one or more findings, and optional related snippets.",
        "Generate zero or more small, safe structured edits for that local patch set.",
        "",
        "## Output Format",
        "You MUST respond with valid JSON in this exact structure:",
        "{",
        '  "edits": [',
        "    {",
        '      "finding_indices": [1],',
        '      "file": "path/to/file.ext",',
        '      "line": 42,',
        '      "end_line": 45,',
        '      "message": "Short patch summary",',
        '      "old_code": "exact code to replace",',
        '      "suggestion": "exact replacement code"',
        "    }",
        "  ]",
        "}",
        "",
        "## Rules",
        "- edits may be empty if no safe exact patch can be produced",
        "- finding_indices must reference the numbered findings from the user message",
        "- old_code must be copied EXACTLY from the target file with original indentation",
        "- Prefer the SMALLEST unique exact snippet that identifies the change safely",
        "- suggestion must be valid, syntactically correct code only",
        "- NEVER return natural-language instructions in old_code or suggestion",
        "- Keep the edit SMALL and SURGICAL (max about 30 changed lines)",
        '- To delete code, set suggestion to an empty string ("")',
        "- If one change safely fixes multiple findings, reference all of them in one finding_indices array",
        "- If you cannot produce a safe exact edit for a finding, omit it instead of guessing",
        "- Respond with JSON only",
    ]
    if comments:
        parts.append("- Add one short BoomAI comment on the first changed line")
    else:
        parts.append("- Do NOT add comments or annotations")
    return append_fix_pack_guidance("\n".join(parts), selected_pack_ids)


def build_fix_user_message(
    findings: list,
    static_hints: list[IssueSeed],
    target_file: str,
    target_content: str,
    target_context_label: str,
    related_snippets: list[ContextSnippet] | None = None,
) -> str:
    """Build user message for grouped patch generation."""
    related_snippets = related_snippets or []

    related_context_text = ""
    if related_snippets:
        lines = ["## Related Cross-File Context"]
        for snippet in related_snippets:
            lines.append(f"### Context: {snippet.file}")
            lines.append(f"Reason: {snippet.reason}")
            lines.append("```")
            lines.append(snippet.content)
            lines.append("```")
            lines.append("")
        related_context_text = "\n".join(lines).rstrip()

    finding_lines = ["## Findings To Patch"]
    static_hint_map = {(hint.file, hint.line): hint for hint in static_hints}
    for index, review_finding in enumerate(findings, 1):
        hint = static_hint_map.get((review_finding.file, review_finding.line))
        finding_lines.append(
            f"{index}. {review_finding.file}:{review_finding.line} "
            f"[{review_finding.severity.value}] {review_finding.body}"
        )
        if hint is not None:
            finding_lines.append(
                f"   Static hint: [{hint.source.value}/{hint.severity.value}] {hint.rule_id}: {hint.message}"
            )
    findings_text = "\n".join(finding_lines)

    return f"""## Local Patch Set Generation

{findings_text}

## Target File Context
### File: {target_file} ({target_context_label})
```
{target_content}
```

{related_context_text}

Generate zero or more exact structured edits for this patch set.
Use ONLY the target file context above when copying old_code."""


def build_plan_prompt(char_budget: int) -> str:
    """Build system prompt for the scan planning phase."""
    return f"""You are BoomAI's planning module. You receive a directory map showing
each folder with its file count and total character size.

Your job: group **directories** into review chunks for an AI code reviewer.

## Rules
- Each chunk MUST stay under {char_budget:,} characters total
- Group related directories together (same subsystem, feature area, or module)
- Put directories with the largest/most complex code in smaller chunks so they get full reviewer attention
- Every directory in the map MUST appear in exactly one chunk - do not skip any
- Order chunks so the most important/complex ones come first
- If a single directory exceeds the budget, put it alone in its own chunk

## Output Format
Respond with valid JSON only:
{{
  "chunks": [
    {{
      "dirs": ["Assets/Scripts/Game/Player/", "Assets/Scripts/Game/Items/"],
      "focus": "Player systems and inventory"
    }}
  ]
}}

IMPORTANT: Use the exact directory paths from the map. Include the trailing slash."""


def build_plan_user_message(repo_map: str, total_files: int, total_chars: int) -> str:
    """Build user message for the scan planning phase."""
    return f"""## Directory Map ({total_files} files, {total_chars:,} characters total)

{repo_map}

Group these directories into review chunks following the rules in the system prompt."""


def build_scan_user_message(
    file_contents: list[tuple[str, str]],
    detected_languages: list[str],
    chunk_info: str = "",
    issue_seeds: list[IssueSeed] | None = None,
    related_snippets: list[ContextSnippet] | None = None,
) -> str:
    """Build user message for full-codebase scan with file contents."""
    lang_configs = [LANGUAGES[k] for k in detected_languages if k in LANGUAGES]
    lang_names = (
        ", ".join(c.name for c in lang_configs)
        if lang_configs
        else "the codebase"
    )

    files_block = []
    for filepath, content in file_contents:
        files_block.append(f"### File: {filepath}")
        files_block.append(f"```\n{content}\n```")
        files_block.append("")
    files_text = "\n".join(files_block)

    issue_seeds = issue_seeds or []
    related_snippets = related_snippets or []
    extra_bullets = "\n".join(_build_language_extras(lang_configs))

    header = "## Full Codebase Scan"
    if chunk_info:
        header += f" ({chunk_info})"

    static_findings_text = ""
    if issue_seeds:
        lines = ["## Static Analysis Findings"]
        for seed in issue_seeds:
            lines.append(
                f"- {seed.file}:{seed.line} [{seed.source.value}/{seed.severity.value}] "
                f"{seed.rule_id}: {seed.message}"
            )
        static_findings_text = "\n".join(lines)

    related_context_text = ""
    if related_snippets:
        lines = ["## Related Cross-File Context"]
        for snippet in related_snippets:
            lines.append(f"### Context: {snippet.file}")
            lines.append(f"Reason: {snippet.reason}")
            lines.append("```")
            lines.append(snippet.content)
            lines.append("```")
            lines.append("")
        related_context_text = "\n".join(lines).rstrip()

    return f"""{header}

{files_text}

{static_findings_text}

{related_context_text}

## Instructions
1. Review ALL the source files above (languages detected: {lang_names})
2. Use any static findings and related cross-file context to validate real issues and avoid false positives
3. Find issues, especially:
{extra_bullets}
4. Focus on bugs, security issues, and correctness - not just style
5. Provide your review as JSON per the system prompt format"""
