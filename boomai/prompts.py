"""Dynamic prompt builder for multi-language code review."""

from boomai.languages import LANGUAGES


def _build_language_extras(lang_configs: list) -> list[str]:
    """Build language-specific review focus bullets (shared by review + scan)."""
    extra = []
    for config in lang_configs:
        if config.name == "C#/Unity":
            extra.append("   - Unity performance anti-patterns")
            extra.append("   - Memory allocation in hot paths")
            extra.append("   - Threading safety issues")
        elif config.name == "TypeScript":
            extra.append("   - Type safety gaps and `any` casts")
            extra.append("   - React rendering performance issues")
            extra.append("   - Unhandled promise rejections")
        elif config.name == "JavaScript":
            extra.append("   - Prototype and closure pitfalls")
            extra.append("   - Unhandled async errors")
        elif config.name == "Python":
            extra.append("   - Type hint correctness")
            extra.append("   - Resource leaks (unclosed files, connections)")
            extra.append("   - Security issues (injection, unsafe deserialization)")
        else:
            extra.append(f"   - {config.name}-specific best practice violations")
    extra.append("   - Merge conflict artifacts and duplicate definitions")
    extra.append("   - Architecture/design concerns")
    return extra


# ============================================================
#  Diff-based review prompts (existing)
# ============================================================

def build_system_prompt(detected_languages: list[str]) -> str:
    """Build a system prompt tailored to the detected languages."""
    lang_configs = [LANGUAGES[k] for k in detected_languages if k in LANGUAGES]
    lang_names = (
        ", ".join(c.name for c in lang_configs)
        if lang_configs
        else "general programming"
    )

    parts = [
        f"You are BoomAI, an expert code reviewer with deep expertise in {lang_names}.",
        "You review pull request diffs and static analysis findings.",
        "",
        "## Your Role",
        "- Validate static analysis findings (confirm real issues, flag false positives)",
        "- Find additional issues the static tools missed",
        "- Focus on language-specific performance, safety, and best practice concerns",
        "- Provide actionable suggestions with corrected code",
    ]

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
        '  "summary": "Brief overall assessment of the PR (2-3 sentences)",',
        '  "findings": [',
        "    {",
        '      "file": "path/to/file.ext",',
        '      "line": 42,',
        '      "severity": "high",',
        '      "message": "Clear explanation of the issue and WHY it matters",',
        '      "old_code": "const x = foo.split(\\\':\\\')",',
        '      "suggestion": "const [key, ...rest] = foo.split(\\\':\\\')\\nconst x = rest.join(\\\':\\\')"',
        "    }",
        "  ],",
        '  "critical_count": 0',
        "}",
        "",
        "## Rules",
        "- severity must be one of: critical, high, medium, low, info",
        "- line numbers are for reference only (to help locate the issue)",
        "",
        "## Suggestion Rules (CRITICAL — read carefully)",
        "- old_code: copy-paste the EXACT code that needs to be replaced (as it appears in the file, with original indentation)",
        "- suggestion: the EXACT replacement code that should replace old_code",
        "- Both old_code and suggestion must be valid, syntactically correct code",
        "- NEVER put natural language instructions in old_code or suggestion",
        "- Keep fixes SMALL and SURGICAL — fix ONE thing at a time (max ~30 lines)",
        '- To DELETE dead/duplicate code, provide old_code and set suggestion to an empty string ("")',
        "- If a fix requires very large restructuring (more than ~30 lines), describe it in `message` and OMIT old_code/suggestion",
        "- If you cannot provide an exact fix, omit both old_code and suggestion",
        "",
        "- Keep findings focused and actionable (max 15 per review)",
        "- Do NOT repeat findings already reported by static analysis unless you have additional context",
        "- If the code looks good, say so in the summary with minimal/no findings",
        "- ALWAYS respond with valid JSON, nothing else",
    ])

    return "\n".join(parts)


def build_user_message(
    diff: str,
    finding_count: int,
    findings_json: str,
    detected_languages: list[str],
) -> str:
    """Build the user message with language-aware review instructions."""
    lang_configs = [LANGUAGES[k] for k in detected_languages if k in LANGUAGES]
    lang_names = (
        ", ".join(c.name for c in lang_configs)
        if lang_configs
        else "the codebase"
    )

    extra_bullets = "\n".join(_build_language_extras(lang_configs))

    return f"""## Pull Request Diff

{diff}

## Static Analysis Findings (Top {finding_count})

{findings_json}

## Instructions
1. Review the diff above (languages detected: {lang_names})
2. Validate the static analysis findings (are they real issues?)
3. Find additional issues the tools missed, especially:
{extra_bullets}
4. Provide your review as JSON per the system prompt format"""


# ============================================================
#  Full-codebase scan prompts (new)
# ============================================================

def build_scan_system_prompt(detected_languages: list[str]) -> str:
    """Build system prompt for full-codebase scan mode."""
    lang_configs = [LANGUAGES[k] for k in detected_languages if k in LANGUAGES]
    lang_names = (
        ", ".join(c.name for c in lang_configs)
        if lang_configs
        else "general programming"
    )

    parts = [
        f"You are BoomAI, an expert code reviewer with deep expertise in {lang_names}.",
        "You are performing a FULL CODEBASE SCAN — reviewing entire source files, not a diff.",
        "",
        "## Your Role",
        "- Identify bugs, security vulnerabilities, and correctness issues",
        "- Find architectural problems and code smells",
        "- Spot performance anti-patterns and resource leaks",
        "- Check for missing error handling and edge cases",
        "- Validate static analysis findings (confirm real issues, flag false positives)",
        "- Provide actionable suggestions with corrected code",
        "",
        "## Focus Areas",
        "- Cross-file inconsistencies (naming, patterns, error handling)",
        "- Dead code and unused imports",
        "- Missing input validation and boundary checks",
        "- Concurrency and thread safety issues",
        "- Resource management (unclosed handles, missing cleanup)",
        "- Merge conflict artifacts and duplicate function/class definitions",
    ]

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
        '      "old_code": "const x = foo.split(\\\':\\\')",',
        '      "suggestion": "const [key, ...rest] = foo.split(\\\':\\\')\\nconst x = rest.join(\\\':\\\')"',
        "    }",
        "  ],",
        '  "critical_count": 0',
        "}",
        "",
        "## Rules",
        "- severity must be one of: critical, high, medium, low, info",
        "- line numbers are for reference only (to help locate the issue)",
        "",
        "## Suggestion Rules (CRITICAL — read carefully)",
        "- old_code: copy-paste the EXACT code that needs to be replaced (as it appears in the file, with original indentation)",
        "- suggestion: the EXACT replacement code that should replace old_code",
        "- Both old_code and suggestion must be valid, syntactically correct code",
        "- NEVER put natural language instructions in old_code or suggestion",
        "- Keep fixes SMALL and SURGICAL — fix ONE thing at a time (max ~30 lines)",
        '- To DELETE dead/duplicate code, provide old_code and set suggestion to an empty string ("")',
        "- If a fix requires very large restructuring (more than ~30 lines), describe it in `message` and OMIT old_code/suggestion",
        "- If you cannot provide an exact fix, omit both old_code and suggestion",
        "",
        "- Keep findings focused and actionable (max 20 per chunk)",
        "- Prioritize high-severity issues over style nits",
        "- If the code looks good, say so in the summary with minimal/no findings",
        "- ALWAYS respond with valid JSON, nothing else",
    ])

    return "\n".join(parts)


def build_scan_user_message(
    file_contents: list[tuple[str, str]],
    finding_count: int,
    findings_json: str,
    detected_languages: list[str],
    chunk_info: str = "",
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

    extra_bullets = "\n".join(_build_language_extras(lang_configs))

    header = "## Full Codebase Scan"
    if chunk_info:
        header += f" ({chunk_info})"

    return f"""{header}

{files_text}

## Static Analysis Findings (Top {finding_count})

{findings_json}

## Instructions
1. Review ALL the source files above (languages detected: {lang_names})
2. Validate the static analysis findings (are they real issues?)
3. Find additional issues the tools missed, especially:
{extra_bullets}
4. Focus on bugs, security issues, and correctness — not just style
5. Provide your review as JSON per the system prompt format"""
