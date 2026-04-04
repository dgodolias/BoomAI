"""Shared orchestration helpers for BoomAI scans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..analysis.services.finding_prioritizer import FindingPrioritizer
from ..analysis.static_analysis import (
    run_devskim,
    run_gitleaks,
    run_roslyn_build,
    run_semgrep,
)
from ..core.models import Finding, IssueSeed
from ..core.policies import build_static_analysis_policy

ProgressFn = Callable[[str], None] | None


@dataclass(frozen=True)
class StaticAnalysisResult:
    findings: list[Finding]
    prioritized_issue_seeds: list[IssueSeed]
    tool_statuses: dict[str, str]


def _emit(emit: ProgressFn, message: str) -> None:
    if emit:
        emit(message)


def run_static_analysis_suite(
    repo_path: str,
    reviewable_files: list[str],
    detected_languages: list[str],
    on_progress: ProgressFn = None,
) -> StaticAnalysisResult:
    """Run all configured static analyzers and normalize their findings."""
    prioritizer = FindingPrioritizer()
    static_analysis_policy = build_static_analysis_policy()
    tools = [
        ("Semgrep", lambda: run_semgrep(repo_path, detected_languages, reviewable_files)),
        ("DevSkim", lambda: run_devskim(repo_path, reviewable_files)),
        ("Roslyn", lambda: run_roslyn_build(repo_path, reviewable_files)),
        ("Gitleaks", lambda: run_gitleaks(repo_path, reviewable_files)),
    ]

    all_findings: list[Finding] = []
    statuses: dict[str, str] = {}

    _emit(on_progress, "Running static analysis...")
    for name, runner in tools:
        findings, status = runner()
        statuses[name] = status
        all_findings.extend(findings)
        _emit(on_progress, f"  {name}: {status}")

    prioritized = [
        IssueSeed(**finding.model_dump(exclude={"suggestion"}))
        for finding in prioritizer.prioritize(
            all_findings,
            max_count=static_analysis_policy.max_issue_seeds,
        )
    ]

    return StaticAnalysisResult(
        findings=all_findings,
        prioritized_issue_seeds=prioritized,
        tool_statuses=statuses,
    )
