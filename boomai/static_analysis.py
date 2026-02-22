import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .models import Finding, Severity, FindingSource
from .languages import LANGUAGES

logger = logging.getLogger(__name__)


import shutil as _shutil

_EXTRA_TOOL_DIRS = [
    Path.home() / ".dotnet" / "tools",
    Path.home() / ".boomai" / "tools",
]


def _find_tool(name: str) -> str:
    """Find a tool binary, checking extra tool directories beyond system PATH."""
    # First check system PATH
    found = _shutil.which(name)
    if found:
        return found
    # Check extra tool directories
    old_path = os.environ.get("PATH", "")
    try:
        extra = os.pathsep.join(str(p) for p in _EXTRA_TOOL_DIRS if p.exists())
        os.environ["PATH"] = f"{extra}{os.pathsep}{old_path}"
        found = _shutil.which(name)
    finally:
        os.environ["PATH"] = old_path
    return found or name  # fallback to bare name (will raise FileNotFoundError)

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
) -> tuple[list[Finding], str]:
    """Run Semgrep with appropriate rulesets based on detected languages."""
    if not changed_files:
        logger.info("No reviewable files, skipping Semgrep")
        return [], "skipped (no files)"

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
        return [], "skipped (no rulesets for detected languages)"

    cmd = ["semgrep", *config_args, "--json", "--no-git-ignore"] + changed_files

    try:
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(cmd, capture_output=True, timeout=120, env=env)
        stdout = result.stdout.decode("utf-8", errors="replace")
        if result.returncode not in (0, 1):
            stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                logger.warning(f"Semgrep stderr: {stderr_text}")
            # Try parsing JSON anyway — warnings can cause exit code 2
            try:
                data = json.loads(stdout)
                if "results" not in data:
                    return [], f"error (exit code {result.returncode})"
            except (json.JSONDecodeError, ValueError):
                return [], f"error (exit code {result.returncode})"
        else:
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
        return findings, f"{len(findings)} finding(s)"
    except subprocess.TimeoutExpired:
        logger.error("Semgrep timed out")
        return [], "timed out"
    except FileNotFoundError:
        return [], "not installed"
    except Exception as e:
        logger.error(f"Semgrep failed: {e}")
        return [], f"error ({e})"


def run_devskim(repo_path: str, files: list[str]) -> tuple[list[Finding], str]:
    """Run DevSkim on repo_path. Returns ([], status) if not installed or fails."""
    if not files:
        return [], "skipped (no files)"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sarif", delete=False) as tmp:
            tmp_path = tmp.name
        result = subprocess.run(
            [
                _find_tool("devskim"), "analyze",
                "--source-code", repo_path,
                "--output-file", tmp_path,
                "--output-format", "Sarif",
            ],
            capture_output=True, timeout=60,
        )
        if result.returncode not in (0, 1):
            return [], f"error (exit code {result.returncode})"
        with open(tmp_path, encoding="utf-8") as fh:
            sarif_data = json.load(fh)
        findings = _parse_sarif(sarif_data, FindingSource.DEVSKIM, files, repo_path)
        logger.info(f"DevSkim found {len(findings)} issue(s)")
        return findings, f"{len(findings)} finding(s)"
    except FileNotFoundError:
        return [], "not installed (dotnet tool install -g Microsoft.CST.DevSkim.CLI)"
    except subprocess.TimeoutExpired:
        return [], "timed out"
    except Exception as e:
        logger.warning(f"DevSkim skipped: {e}")
        return [], f"error ({e})"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def run_roslyn_build(repo_path: str, files: list[str]) -> tuple[list[Finding], str]:
    """Try dotnet build with Roslyn analyzers. Returns ([], status) if no project or fails."""
    if not files:
        return [], "skipped (no files)"
    project_file = None
    for pattern in ["*.sln", "**/*.sln", "*.csproj", "**/*.csproj"]:
        matches = sorted(Path(repo_path).glob(pattern))
        if matches:
            project_file = str(matches[0])
            break
    if not project_file:
        return [], "skipped (no .sln/.csproj found)"
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
        return findings, f"{len(findings)} finding(s)"
    except FileNotFoundError:
        return [], "not installed (requires .NET SDK)"
    except subprocess.TimeoutExpired:
        logger.warning("Roslyn build timed out")
        return [], "timed out"
    except Exception as e:
        logger.warning(f"Roslyn build skipped: {e}")
        return [], f"error ({e})"


def run_gitleaks(repo_path: str, files: list[str]) -> tuple[list[Finding], str]:
    """Run Gitleaks to detect secrets/credentials. Returns ([], status) if not installed."""
    if not files:
        return [], "skipped (no files)"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name
        result = subprocess.run(
            [
                _find_tool("gitleaks"), "detect",
                "--source", repo_path,
                "--report-format", "json",
                "--report-path", tmp_path,
                "--no-git",
            ],
            capture_output=True, timeout=120,
        )
        # gitleaks: exit 0 = no leaks, exit 1 = leaks found, others = error
        if result.returncode not in (0, 1):
            return [], f"error (exit code {result.returncode})"
        with open(tmp_path, encoding="utf-8") as fh:
            content = fh.read().strip()
        if not content or content == "[]":
            return [], "0 finding(s)"
        data = json.loads(content)
        findings = []
        file_set = set(f.replace("\\", "/") for f in files)
        for leak in data:
            leak_file = leak.get("File", "").replace("\\", "/")
            # Match against our target files
            matched = None
            for f in file_set:
                if leak_file.endswith("/" + f) or leak_file == f:
                    matched = f.replace("/", "\\") if "\\" in files[0] else f
                    break
            if matched is None:
                continue
            findings.append(Finding(
                file=matched,
                line=leak.get("StartLine", 1),
                severity=Severity.CRITICAL,
                source=FindingSource.GITLEAKS,
                rule_id=leak.get("RuleID", "secret-detected"),
                message=f"Secret detected: {leak.get('Description', 'potential secret/credential')}",
            ))
        logger.info(f"Gitleaks found {len(findings)} issue(s)")
        return findings, f"{len(findings)} finding(s)"
    except FileNotFoundError:
        return [], "not installed"
    except subprocess.TimeoutExpired:
        return [], "timed out"
    except Exception as e:
        logger.warning(f"Gitleaks skipped: {e}")
        return [], f"error ({e})"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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
