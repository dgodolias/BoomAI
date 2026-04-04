from __future__ import annotations

from ...analysis.static_analysis import prioritize_findings
from ...core.models import Finding


class FindingPrioritizer:
    """Encapsulates selection of diverse, high-value static-analysis findings."""

    def prioritize(self, findings: list[Finding], max_count: int) -> list[Finding]:
        return prioritize_findings(findings, max_count=max_count)
