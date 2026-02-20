import json
import logging
import subprocess
from pathlib import Path

from scripts.models import Finding, Severity, FindingSource
from scripts.languages import LANGUAGES

logger = logging.getLogger(__name__)


def run_semgrep(
    changed_files: list[str], detected_languages: list[str]
) -> list[Finding]:
    """Run Semgrep with appropriate rulesets based on detected languages."""
    if not changed_files:
        logger.info("No reviewable files, skipping Semgrep")
        return []

    rules_dir = Path(__file__).parent.parent / "rules" / "semgrep"

    # Build --config flags dynamically from detected languages
    config_args = []
    for lang_key in detected_languages:
        config = LANGUAGES.get(lang_key)
        if not config:
            continue
        for ruleset in config.semgrep_rulesets:
            config_args.extend(["--config", ruleset])
        if config.custom_rules_file:
            custom_path = rules_dir / config.custom_rules_file
            if custom_path.exists():
                config_args.extend(["--config", str(custom_path)])

    if not config_args:
        logger.info("No Semgrep rulesets for detected languages, skipping")
        return []

    cmd = [
        "semgrep",
        *config_args,
        "--json",
        "--no-git-ignore",
    ] + changed_files

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
