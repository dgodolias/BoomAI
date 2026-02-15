import json
import logging
import re
import subprocess
from pathlib import Path

from scripts.models import Finding, Severity, FindingSource

logger = logging.getLogger(__name__)


def run_semgrep(changed_files: list[str]) -> list[Finding]:
    """Run Semgrep with custom Unity rules on changed C# files."""
    rules_dir = Path(__file__).parent.parent / "rules" / "semgrep"

    cs_files = [f for f in changed_files if f.endswith(".cs")]
    if not cs_files:
        logger.info("No C# files changed, skipping Semgrep")
        return []

    cmd = [
        "semgrep",
        "--config", str(rules_dir),
        "--config", "p/csharp",
        "--json",
        "--no-git-ignore",
    ] + cs_files

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode not in (0, 1):  # 1 = findings found (normal)
            logger.error(f"Semgrep error: {result.stderr}")
            return []

        data = json.loads(result.stdout)
        findings = []
        severity_map = {
            "ERROR": Severity.HIGH,
            "WARNING": Severity.MEDIUM,
            "INFO": Severity.LOW,
        }
        for r in data.get("results", []):
            findings.append(Finding(
                file=r["path"],
                line=r["start"]["line"],
                end_line=r["end"]["line"],
                severity=severity_map.get(r["extra"]["severity"], Severity.MEDIUM),
                source=FindingSource.SEMGREP,
                rule_id=r["check_id"],
                message=r["extra"]["message"],
            ))
        logger.info(f"Semgrep found {len(findings)} issue(s)")
        return findings

    except subprocess.TimeoutExpired:
        logger.error("Semgrep timed out")
        return []
    except Exception as e:
        logger.error(f"Semgrep failed: {e}")
        return []


def run_dotnet_analysis(solution_path: str | None = None) -> list[Finding]:
    """Run dotnet build with analyzers and parse MSBuild warnings/errors."""
    if not solution_path:
        logger.info("No .sln file found, skipping Roslyn analysis")
        return []

    cmd = [
        "dotnet", "build", solution_path,
        "--no-restore",
        "/p:TreatWarningsAsErrors=false",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        findings = []
        pattern = r"(.+?)\((\d+),\d+\): (warning|error) (\w+): (.+)"
        for line in result.stdout.splitlines() + result.stderr.splitlines():
            m = re.match(pattern, line)
            if m:
                findings.append(Finding(
                    file=m.group(1).strip(),
                    line=int(m.group(2)),
                    severity=Severity.HIGH if m.group(3) == "error" else Severity.MEDIUM,
                    source=FindingSource.ROSLYN,
                    rule_id=m.group(4),
                    message=m.group(5).strip(),
                ))
        logger.info(f"Roslyn found {len(findings)} issue(s)")
        return findings

    except subprocess.TimeoutExpired:
        logger.error("Roslyn analysis timed out")
        return []
    except Exception as e:
        logger.error(f"Roslyn analysis failed: {e}")
        return []


def filter_to_changed_files(
    findings: list[Finding], changed_files: list[str]
) -> list[Finding]:
    """Only keep findings in files that are part of the PR diff."""
    changed_set = set(changed_files)
    return [f for f in findings if f.file in changed_set]


def prioritize_findings(
    findings: list[Finding], max_count: int = 20
) -> list[Finding]:
    """Sort by severity and return top N findings."""
    severity_order = {
        Severity.CRITICAL: 0,
        Severity.HIGH: 1,
        Severity.MEDIUM: 2,
        Severity.LOW: 3,
        Severity.INFO: 4,
    }
    sorted_findings = sorted(findings, key=lambda f: severity_order[f.severity])
    return sorted_findings[:max_count]
