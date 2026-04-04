from __future__ import annotations

import re
from collections import Counter

from ...core.models import ReviewComment, Severity


def is_unavailable_summary(summary: str) -> bool:
    return summary in {"AI review unavailable.", "Split required."}


def summary_theme_for_finding(finding: ReviewComment) -> str:
    category = (finding.category or "").strip().lower()
    if category in {
        "correctness",
        "security",
        "lifecycle",
        "resource",
        "threading",
        "bounds",
        "data-integrity",
        "api-contract",
    }:
        category_map = {
            "resource": "resource management",
            "threading": "thread safety",
            "bounds": "bounds safety",
            "data-integrity": "data integrity",
            "api-contract": "API contract handling",
        }
        return category_map.get(category, category)

    lowered = finding.body.lower()
    if any(token in lowered for token in (
        "allocation", "allocates", "gc pressure", "waitforseconds", "waitforendofframe",
        "camera.main", "resources.load", "hot path", "mesh update", "per-frame",
        "string concatenation", "string formatting",
    )):
        return "performance"
    if any(token in lowered for token in (
        "null", "ondestroy", "ondisable", "subscription", "unsubscribe",
        "dontdestroyonload", "lifecycle", "destroy", "cleanup", "leak",
    )):
        return "lifecycle"
    if any(token in lowered for token in (
        "assignment operator", "equality", "logic", "off-by-one", "indexoutofrange",
        "argumentoutofrange", "duplicate", "bounds", "infinite", "out of range",
    )):
        return "correctness"
    if any(token in lowered for token in ("shared material", "dispose", "memory leak", "resource")):
        return "resource management"
    return "reliability"


def join_summary_labels(labels: list[str]) -> str:
    labels = [label for label in labels if label]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{', '.join(labels[:-1])}, and {labels[-1]}"


def dedupe_summaries(summaries: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for summary in summaries:
        normalized = re.sub(r"\s+", " ", summary.strip().lower())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(summary.strip())
    return unique


def synthesize_summary_from_findings(
    findings: list[ReviewComment],
    summaries: list[str],
) -> str | None:
    if not findings:
        return None

    total = len(findings)
    high_or_critical = sum(1 for item in findings if item.severity in {Severity.CRITICAL, Severity.HIGH})
    theme_counts = Counter(summary_theme_for_finding(item) for item in findings)
    top_themes = [theme for theme, _ in theme_counts.most_common(3)]

    file_counts = Counter(item.file.rsplit("/", 1)[-1] for item in findings)
    hotspot_files = [name for name, count in file_counts.most_common(2) if count > 1]

    unique_summaries = dedupe_summaries(
        [summary for summary in summaries if summary and not is_unavailable_summary(summary)]
    )
    lead_fragment = ""
    if unique_summaries:
        lead = re.sub(r"^The codebase\s+", "", unique_summaries[0], flags=re.IGNORECASE).rstrip(". ")
        if lead:
            lead_fragment = lead[:1].lower() + lead[1:] if len(lead) > 1 else lead.lower()

    parts: list[str] = []
    if high_or_critical:
        parts.append(
            f"The review found {total} issues, including {high_or_critical} high-severity problem"
            f"{'' if high_or_critical == 1 else 's'}."
        )
    else:
        parts.append(f"The review found {total} issues.")

    if top_themes:
        parts.append(f"The main themes were {join_summary_labels(top_themes)}.")

    if hotspot_files:
        parts.append(f"The heaviest hotspots were {join_summary_labels(hotspot_files)}.")

    if lead_fragment and lead_fragment not in " ".join(parts).lower():
        parts.append(lead_fragment[:1].upper() + lead_fragment[1:] + ".")

    return " ".join(parts[:3])


def combine_review_summaries(
    summaries: list[str],
    *,
    findings: list[ReviewComment] | None = None,
    fallback: str = "AI review unavailable.",
    force_recovered_text: bool = False,
) -> str:
    """Combine summaries while suppressing internal placeholders."""
    informative = [summary for summary in summaries if summary and not is_unavailable_summary(summary)]
    if findings:
        synthesized = synthesize_summary_from_findings(findings, informative)
        if synthesized:
            return synthesized
    if informative:
        unique = dedupe_summaries(informative)
        if len(unique) == 1:
            return unique[0]
        return " ".join(unique[:2])
    if force_recovered_text:
        return "Review completed from split sub-chunks."
    if "Rate limited." in summaries:
        return "Rate limited."
    return fallback
