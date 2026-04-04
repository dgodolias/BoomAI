from __future__ import annotations

import logging

from boomai.core.models import ReviewComment, ReviewSummary, Severity
from boomai.review.services.finding_policy import filter_findings, is_fix_worthy
from boomai.review.services.patch_batch_generator import group_actionable_findings
from boomai.review.services.response_parser import parse_review_response, sanitize_json
from boomai.review.services.summary_synthesizer import combine_review_summaries


def test_sanitize_json_removes_trailing_commas() -> None:
    raw = '{"summary":"ok","findings":[{"file":"a.py","line":1,"severity":"medium","message":"x"},],}'
    sanitized = sanitize_json(raw)
    assert sanitized == '{"summary":"ok","findings":[{"file":"a.py","line":1,"severity":"medium","message":"x"}]}'


def test_parse_review_response_parses_valid_json() -> None:
    raw = (
        '{"summary":"done","findings":['
        '{"file":"a.py","line":1,"severity":"high","message":"broken"}'
        '],"critical_count":1}'
    )

    def fallback() -> ReviewSummary:
        return ReviewSummary(summary="fallback", findings=[])

    parsed, status = parse_review_response(
        raw,
        debug=False,
        logger=logging.getLogger("test"),
        fallback_review=fallback,
    )
    assert status == "full"
    assert parsed.summary == "done"
    assert parsed.critical_count == 1
    assert parsed.findings[0].file == "a.py"


def test_finding_policy_filters_low_signal_findings() -> None:
    review = ReviewSummary(
        summary="x",
        findings=[
            ReviewComment(
                file="a.py",
                line=1,
                severity=Severity.MEDIUM,
                body="redundant ternary should be simplified",
            ),
            ReviewComment(
                file="a.py",
                line=5,
                severity=Severity.HIGH,
                body="missing null check can cause runtime crash",
            ),
        ],
    )
    filtered = filter_findings(review)
    assert len(filtered.findings) == 1
    assert filtered.findings[0].line == 5
    assert is_fix_worthy(filtered.findings[0]) is True


def test_group_actionable_findings_groups_nearby_lines() -> None:
    findings = [
        ReviewComment(file="a.py", line=10, severity=Severity.MEDIUM, body="one"),
        ReviewComment(file="a.py", line=18, severity=Severity.MEDIUM, body="two"),
        ReviewComment(file="a.py", line=80, severity=Severity.MEDIUM, body="three"),
    ]
    groups = group_actionable_findings(findings)
    assert len(groups) == 2
    assert [item.line for item in groups[0]] == [10, 18]


def test_combine_review_summaries_synthesizes_from_findings() -> None:
    findings = [
        ReviewComment(file="Assets/A.cs", line=10, severity=Severity.HIGH, body="missing null check"),
        ReviewComment(file="Assets/A.cs", line=14, severity=Severity.MEDIUM, body="memory leak in cleanup"),
        ReviewComment(file="Assets/B.cs", line=22, severity=Severity.MEDIUM, body="off-by-one logic bug"),
    ]
    combined = combine_review_summaries(
        ["The codebase contains runtime issues.", "The codebase contains runtime issues."],
        findings=findings,
    )
    assert "The review found 3 issues" in combined
    assert "A.cs" in combined
