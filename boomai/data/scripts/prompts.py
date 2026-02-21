"""Dynamic prompt builder for multi-language code review."""

from scripts.languages import LANGUAGES


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
        '      "end_line": 45,',
        '      "severity": "high",',
        '      "message": "Clear explanation of the issue and WHY it matters",',
        '      "suggestion": "// corrected code here\\n// can be multi-line"',
        "    }",
        "  ],",
        '  "critical_count": 0',
        "}",
        "",
        "## Rules",
        "- severity must be one of: critical, high, medium, low, info",
        "- line numbers must reference the actual diff line numbers",
        "- suggestion field is optional but preferred when you can provide a fix",
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
    extra.append("   - Architecture/design concerns")

    extra_bullets = "\n".join(extra)

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
