from __future__ import annotations

from ...core.config import settings
from ...core.models import ReviewComment, ReviewSummary, Severity


HIGH_VALUE_CATEGORIES = {
    "correctness",
    "security",
    "resource",
    "lifecycle",
    "threading",
    "bounds",
    "data-integrity",
    "api-contract",
}


def is_fix_worthy(finding: ReviewComment) -> bool:
    """Return True when a finding should get a dedicated patch-generation pass."""
    lowered = finding.body.lower()
    if finding.fixable is False:
        return False
    if finding.fixable is True:
        if finding.severity in {Severity.LOW, Severity.INFO}:
            return False
        if finding.confidence == "low" and finding.severity == Severity.MEDIUM:
            return False
        return True

    noisy_patterns = (
        "incomplete feature",
        "requires confirmation",
        "needs to be confirmed",
        "placeholder",
        "todo",
        "may be a bug",
    )
    if any(pattern in lowered for pattern in noisy_patterns):
        return False

    if finding.severity in {Severity.CRITICAL, Severity.HIGH}:
        return True
    if finding.severity in {Severity.LOW, Severity.INFO}:
        return False

    strong_fix_patterns = (
        "memory leak",
        "unsubscribe",
        "unsubscription",
        "missing ondestroy",
        "missing null check",
        "null reference",
        "duplicate key",
        "off-by-one",
        "invalidcastexception",
        "argumentoutofrange",
        "returns early instead of continue",
        "double remove",
        "bypassing effect cleanup",
        "leaks gameobject",
        "skipping remaining",
    )
    perf_only_patterns = (
        "gc pressure",
        "console spam",
        "string concatenation",
        "linq allocations",
        "list allocation",
        "allocates list",
        "allocates memory",
        "called in hot path",
    )

    if any(pattern in lowered for pattern in strong_fix_patterns):
        return True
    if any(pattern in lowered for pattern in perf_only_patterns):
        return False
    return finding.severity == Severity.MEDIUM


def is_high_value_finding(finding: ReviewComment) -> bool:
    """Filter out low-signal findings so final output stays bug-first."""
    lowered = finding.body.lower()

    if finding.severity in {Severity.CRITICAL, Severity.HIGH}:
        return True

    low_value_patterns = (
        "redundant ternary",
        "return condition directly",
        "redundant count",
        "redundant cast",
        "empty start override",
        "displayname getter allocates new string",
        "active debug.logformat in production code",
        "minor code smell",
    )
    if any(pattern in lowered for pattern in low_value_patterns):
        return False

    if settings.scan_profile == "deep":
        if finding.confidence == "low" and finding.category not in HIGH_VALUE_CATEGORIES:
            return False
        return finding.severity not in {Severity.LOW, Severity.INFO}

    if finding.category in HIGH_VALUE_CATEGORIES:
        if finding.confidence == "low" and finding.severity == Severity.MEDIUM:
            return False
        return finding.severity not in {Severity.LOW, Severity.INFO}

    medium_keep_patterns = (
        "memory leak",
        "unsubscribe",
        "null check",
        "nullreference",
        "invalidoperationexception",
        "invalidcastexception",
        "argumentoutofrange",
        "dividebyzero",
        "duplicate key",
        "logic bug",
        "runtime crash",
        "off-by-one",
        "double remove",
        "bypass",
        "skips",
        "stale state",
        "reflection",
        "resistance",
        "fast travel",
        "frame stutters",
        "disk load",
        "resources.load",
    )
    if any(pattern in lowered for pattern in medium_keep_patterns):
        return True

    return finding.severity == Severity.MEDIUM and is_fix_worthy(finding)


def filter_findings(review: ReviewSummary) -> ReviewSummary:
    """Keep final findings focused on meaningful bugs and actionable risks."""
    kept = [finding for finding in review.findings if is_high_value_finding(finding)]
    if len(kept) == len(review.findings):
        return review
    return ReviewSummary(
        summary=review.summary,
        findings=kept,
        critical_count=review.critical_count,
        has_critical=review.has_critical,
        usage=review.usage,
    )
