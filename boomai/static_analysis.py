import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from boomai.models import Finding, Severity, FindingSource
from boomai.languages import LANGUAGES

logger = logging.getLogger(__name__)

# Regex for dotnet build diagnostic output:
# /path/to/File.cs(42,8): warning CA2000: Description [project.csproj]
_DOTNET_DIAG = re.compile(
    r'^(.+?)\((\d+),\d+\):\s+(warning|error)\s+([\w\d]+):\s+(.+?)(?:\s+\[.+\])?$',
    re.MULTILINE,
)


def _match_file(uri: str, files: list[str], repo_path: str) -> str | None:
    """Match a reported path (SARIF URI or dotnet path) to one of our target files."""
    norm = uri.replace("file:///", "").replace("file://", "").replace("\\", "/")
    for f in files:
        fn = f.replace("\\", "/")
        if norm == fn or norm.endswith("/" + fn):
            return f
    return None


def _parse_sarif(
    sarif_data: dict, source: FindingSource, files: list[str], repo_path: str
) -> list[Finding]:
    """Parse SARIF output and return Findings filtered to target files."""
    findings = []
    level_map = {
        "error": Severity.HIGH,
        "warning": Severity.MEDIUM,
        "note": Severity.LOW,
        "none": Severity.INFO,
    }
    for run in sarif_data.get("runs", []):
        for result in run.get("results", []):
            level = result.get("level", "warning")
            severity = level_map.get(level, Severity.MEDIUM)
            rule_id = result.get("ruleId", "unknown")
            message = result.get("message", {}).get("text", "")
            for location in result.get("locations", []):
                phys = location.get("physicalLocation", {})
                uri = phys.get("artifactLocation", {}).get("uri", "")
                line = phys.get("region", {}).get("startLine", 1)
                matched = _match_file(uri, files, repo_path)
                if matched is None:
                    continue
                findings.append(Finding(
                    file=matched,
                    line=line,
                    severity=severity,
                    source=source,
                    rule_id=rule_id,
                    message=message,
                ))
    return findings


def run_semgrep(
    changed_files: list[str], detected_languages: list[str]
) -> list[Finding]:
    """Run Semgrep with appropriate rulesets based on detected languages."""
    if not changed_files:
        logger.info("No reviewable files, skipping Semgrep")
        return []

    rules_dir = Path(__file__).parent / "data" / "semgrep"

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

    cmd = ["semgrep", *config_args, "--json", "--no-git-ignore"] + changed_files

    try:
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(cmd, capture_output=True, timeout=120, env=env)
        stdout = result.stdout.decode("utf-8", errors="replace")
        if result.returncode not in (0, 1):
            return []
        data = json.loads(stdout)
        findings = []
        severity_map = {"ERROR": Severity.HIGH, "WARNING": Severity.MEDIUM, "INFO": Severity.LOW}
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


def run_devskim(repo_path: str, files: list[str]) -> list[Finding]:
    """Run DevSkim on repo_path. Returns [] if DevSkim is not installed or fails."""
    if not files:
        return []
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sarif", delete=False) as tmp:
            tmp_path = tmp.name
        result = subprocess.run(
            [
                "devskim", "analyze",
                "--source-code", repo_path,
                "--output-file", tmp_path,
                "--output-format", "Sarif",
            ],
            capture_output=True, timeout=60,
        )
        if result.returncode not in (0, 1):
            return []
        with open(tmp_path, encoding="utf-8") as fh:
            sarif_data = json.load(fh)
        findings = _parse_sarif(sarif_data, FindingSource.DEVSKIM, files, repo_path)
        logger.info(f"DevSkim found {len(findings)} issue(s)")
        return findings
    except FileNotFoundError:
        return []  # devskim not installed
    except Exception as e:
        logger.warning(f"DevSkim skipped: {e}")
        return []
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def run_roslyn_build(repo_path: str, files: list[str]) -> list[Finding]:
    """Try dotnet build with Roslyn analyzers. Returns [] if no project found or build fails."""
    if not files:
        return []
    project_file = None
    for pattern in ["*.sln", "**/*.sln", "*.csproj", "**/*.csproj"]:
        matches = sorted(Path(repo_path).glob(pattern))
        if matches:
            project_file = str(matches[0])
            break
    if not project_file:
        return []
    try:
        result = subprocess.run(
            [
                "dotnet", "build", project_file,
                "/p:TreatWarningsAsErrors=false",
                "/p:EnableNETAnalyzers=true",
                "/p:EnforceCodeStyleInBuild=true",
                "--no-restore",
                "-v:minimal",
            ],
            capture_output=True, timeout=180, cwd=repo_path,
        )
        output = (
            result.stdout.decode("utf-8", errors="replace")
            + result.stderr.decode("utf-8", errors="replace")
        )
        findings = []
        severity_map = {"error": Severity.HIGH, "warning": Severity.MEDIUM}
        for m in _DOTNET_DIAG.finditer(output):
            path, line, level, rule_id, message = m.groups()
            matched = _match_file(path, files, repo_path)
            if matched is None:
                continue
            findings.append(Finding(
                file=matched,
                line=int(line),
                severity=severity_map.get(level, Severity.MEDIUM),
                source=FindingSource.ROSLYN,
                rule_id=rule_id,
                message=message.strip(),
            ))
        logger.info(f"Roslyn build found {len(findings)} issue(s)")
        return findings
    except FileNotFoundError:
        return []  # dotnet not installed
    except subprocess.TimeoutExpired:
        logger.warning("Roslyn build timed out")
        return []
    except Exception as e:
        logger.warning(f"Roslyn build skipped: {e}")
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
