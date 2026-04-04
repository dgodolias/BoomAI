import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from ..core.models import Finding, FindingSource, Severity
from ..core.policies import build_static_analysis_policy
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
    repo_path: str, detected_languages: list[str], files: list[str]
) -> tuple[list[Finding], str]:
    """Run Semgrep once on the repo directory with --include for extensions.

    Runs a single Semgrep invocation with --quiet to suppress progress/summary
    noise. Uses --include patterns to limit scanning to reviewable extensions.
    """
    if not files:
        logger.info("No reviewable files, skipping Semgrep")
        return [], "skipped (no files)"

    rules_dir = Path(__file__).resolve().parent.parent / "data" / "semgrep"

    config_args = []
    include_args = []
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
        for ext in config.extensions:
            include_args.extend(["--include", f"*{ext}"])

    if not config_args:
        logger.info("No Semgrep rulesets for detected languages, skipping")
        return [], "skipped (no rulesets for detected languages)"

    cmd = ["semgrep", *config_args, "--json", "--quiet", *include_args, repo_path]

    try:
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(cmd, capture_output=True, timeout=300, env=env)
        stdout = result.stdout.decode("utf-8", errors="replace")

        # Exit codes: 0 = clean, 1 = findings, 2 = warnings (still has valid JSON)
        if result.returncode not in (0, 1, 2):
            stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                logger.warning(f"Semgrep error: {stderr_text[:200]}")
            return [], f"error (exit code {result.returncode})"

        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return [], "error (invalid JSON output)"

        # Build a set of reviewable file paths for filtering
        file_set = set(f.replace("\\", "/") for f in files)

        findings = []
        severity_map = {"ERROR": Severity.HIGH, "WARNING": Severity.MEDIUM, "INFO": Severity.LOW}
        for r in data.get("results", []):
            path = r["path"].replace("\\", "/")
            # Filter to only files in our reviewable set
            matched = None
            for f in file_set:
                if path == f or path.endswith("/" + f):
                    matched = f
                    break
            if matched is None:
                continue
            findings.append(Finding(
                file=matched,
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


_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}

_MESSAGE_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "will", "into",
    "inside", "used", "when", "where", "can", "cause", "causes", "could",
    "should", "while", "through", "without", "before", "after", "inside",
    "outside", "null", "check", "checks", "missing", "potential",
}


def _rule_family(rule_id: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (rule_id or "").lower()).strip("-")
    if not normalized:
        return "unknown"
    parts = [part for part in normalized.split("-") if part]
    if not parts:
        return "unknown"
    return "-".join(parts[-2:]) if len(parts) >= 2 else parts[0]


def _message_key(message: str) -> str:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", (message or "").lower())
        if len(token) > 2 and token not in _MESSAGE_STOPWORDS
    ]
    if not tokens:
        return ""
    return " ".join(tokens[:6])


def _finding_sort_key(finding: Finding) -> tuple:
    return (
        _SEVERITY_ORDER[finding.severity],
        finding.file.lower(),
        finding.line,
        finding.source.value,
        _rule_family(finding.rule_id),
        finding.message.lower(),
    )


def _is_near_duplicate(candidate: Finding, existing: Finding) -> bool:
    if candidate.file != existing.file:
        return False
    if abs(candidate.line - existing.line) > 3:
        return False
    same_rule = _rule_family(candidate.rule_id) == _rule_family(existing.rule_id)
    same_message = (
        _message_key(candidate.message)
        and _message_key(candidate.message) == _message_key(existing.message)
    )
    return same_rule or same_message


def _dedupe_findings(findings: list[Finding]) -> list[Finding]:
    deduped: list[Finding] = []
    for finding in sorted(findings, key=_finding_sort_key):
        if any(_is_near_duplicate(finding, existing) for existing in deduped):
            continue
        deduped.append(finding)
    return deduped


def _select_diverse(
    findings: list[Finding],
    *,
    max_count: int,
    max_per_file: int,
    max_per_rule: int,
    selected: list[Finding] | None = None,
) -> list[Finding]:
    selected = list(selected or [])
    file_counts: dict[str, int] = {}
    rule_counts: dict[tuple[str, str], int] = {}

    for finding in selected:
        file_counts[finding.file] = file_counts.get(finding.file, 0) + 1
        rule_key = (finding.source.value, _rule_family(finding.rule_id))
        rule_counts[rule_key] = rule_counts.get(rule_key, 0) + 1

    remaining = [finding for finding in findings if finding not in selected]

    while remaining and len(selected) < max_count:
        eligible = []
        for finding in remaining:
            rule_key = (finding.source.value, _rule_family(finding.rule_id))
            if file_counts.get(finding.file, 0) >= max_per_file:
                continue
            if rule_counts.get(rule_key, 0) >= max_per_rule:
                continue
            eligible.append(finding)

        if not eligible:
            break

        best = min(
            eligible,
            key=lambda finding: (
                _SEVERITY_ORDER[finding.severity],
                file_counts.get(finding.file, 0),
                rule_counts.get((finding.source.value, _rule_family(finding.rule_id)), 0),
                finding.line,
                finding.file.lower(),
                finding.source.value,
                _rule_family(finding.rule_id),
            ),
        )

        selected.append(best)
        file_counts[best.file] = file_counts.get(best.file, 0) + 1
        best_rule_key = (best.source.value, _rule_family(best.rule_id))
        rule_counts[best_rule_key] = rule_counts.get(best_rule_key, 0) + 1
        remaining.remove(best)

    return selected


def prioritize_findings(
    findings: list[Finding], max_count: int = 20
) -> list[Finding]:
    """Return deduped, severity-aware findings with file/rule diversity."""
    static_analysis_policy = build_static_analysis_policy()
    deduped = _dedupe_findings(findings)
    if len(deduped) <= max_count:
        return sorted(deduped, key=_finding_sort_key)

    sorted_findings = sorted(deduped, key=_finding_sort_key)

    # First pass keeps good spread across files/rule families.
    selected = _select_diverse(
        sorted_findings,
        max_count=max_count,
        max_per_file=static_analysis_policy.initial_file_cap,
        max_per_rule=static_analysis_policy.initial_rule_family_cap,
    )

    # Second pass relaxes quotas so we do not throw away important findings.
    if len(selected) < max_count:
        selected = _select_diverse(
            sorted_findings,
            max_count=max_count,
            max_per_file=static_analysis_policy.relaxed_file_cap,
            max_per_rule=static_analysis_policy.relaxed_rule_family_cap,
            selected=selected,
        )

    # Final fill from remaining severity-sorted findings, if any slots remain.
    if len(selected) < max_count:
        selected_set = set(id(finding) for finding in selected)
        for finding in sorted_findings:
            if id(finding) in selected_set:
                continue
            selected.append(finding)
            selected_set.add(id(finding))
            if len(selected) >= max_count:
                break

    return selected[:max_count]
