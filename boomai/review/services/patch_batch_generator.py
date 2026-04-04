from __future__ import annotations

from ...core.config import settings
from ...core.models import ReviewComment, Severity
from .response_parser import parse_fix_response as parse_fix_response_response_parser


def fix_priority(finding: ReviewComment) -> tuple[int, int]:
    """Sort findings so the most valuable auto-fixes run first."""
    severity_score = {
        Severity.CRITICAL: 4,
        Severity.HIGH: 3,
        Severity.MEDIUM: 2,
        Severity.LOW: 1,
        Severity.INFO: 0,
    }[finding.severity]
    lowered = finding.body.lower()
    safety_bonus = 0
    if any(token in lowered for token in ("memory leak", "null", "duplicate key", "off-by-one", "unsubscribe")):
        safety_bonus = 2
    elif any(token in lowered for token in ("invalidcast", "argumentoutofrange", "cleanup", "continue")):
        safety_bonus = 1
    return (severity_score, safety_bonus)


def extract_patch_context(content: str, line: int) -> tuple[str, str]:
    """Return a bounded line window around the finding for patch generation."""
    lines = content.replace("\r\n", "\n").split("\n")
    if not lines:
        return ("whole file", content)

    radius = max(12, settings.patch_context_lines)
    start = max(0, line - 1 - radius)
    end = min(len(lines), line - 1 + radius + 1)
    snippet = "\n".join(lines[start:end])
    return (f"lines {start + 1}-{end}", snippet)


def extract_patch_context_for_findings(
    content: str,
    findings: list[ReviewComment],
) -> tuple[str, str]:
    """Return one bounded line window covering a local patch set."""
    lines = content.replace("\r\n", "\n").split("\n")
    if not lines or not findings:
        return ("whole file", content)

    radius = max(12, settings.patch_context_lines)
    min_line = min(f.line for f in findings)
    max_line = max((f.end_line or f.line) for f in findings)
    start = max(0, min_line - 1 - radius)
    end = min(len(lines), max_line + radius)
    snippet = "\n".join(lines[start:end])
    return (f"lines {start + 1}-{end}", snippet)


def group_actionable_findings(findings: list[ReviewComment]) -> list[list[ReviewComment]]:
    """Group findings by file and nearby patch-set so one API call can fix several."""
    if not findings:
        return []

    by_file: dict[str, list[ReviewComment]] = {}
    for finding in findings:
        by_file.setdefault(finding.file, []).append(finding)

    groups: list[list[ReviewComment]] = []
    proximity_threshold = max(20, settings.patch_context_lines // 2)

    for file_findings in by_file.values():
        file_findings.sort(key=lambda item: (item.line, item.end_line or item.line, item.body))
        keyed: dict[str, list[ReviewComment]] = {}
        unkeyed: list[ReviewComment] = []
        for finding in file_findings:
            key = (finding.patch_group_key or "").strip()
            if key:
                keyed.setdefault(key, []).append(finding)
            else:
                unkeyed.append(finding)

        groups.extend(keyed.values())

        current: list[ReviewComment] = []
        current_end = -1
        for finding in unkeyed:
            finding_start = finding.line
            finding_end = finding.end_line or finding.line
            if not current:
                current = [finding]
                current_end = finding_end
                continue
            if finding_start - current_end <= proximity_threshold and len(current) < 4:
                current.append(finding)
                current_end = max(current_end, finding_end)
                continue
            groups.append(current)
            current = [finding]
            current_end = finding_end
        if current:
            groups.append(current)

    def group_sort_key(group: list[ReviewComment]) -> tuple[int, int, str, int]:
        top_priority = max((fix_priority(item) for item in group), default=(0, 0))
        severity_score, safety_bonus = top_priority
        return (-severity_score, -safety_bonus, group[0].file, group[0].line)

    groups.sort(key=group_sort_key)
    return groups


def parse_fix_response(
    text: str,
    default_findings: list[ReviewComment],
) -> list[tuple[list[int], ReviewComment]]:
    """Compatibility parser for grouped patch responses."""
    return parse_fix_response_response_parser(text, default_findings)
