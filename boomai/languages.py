"""Language detection and configuration for multi-language code review."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class LanguageConfig:
    name: str
    extensions: list[str]
    semgrep_rulesets: list[str]
    custom_rules_file: str | None = None
    expertise: str = ""
    prompt_extras: list[str] = field(default_factory=list)


# ============================================================
#  Built-in languages (always available, no YAML needed)
# ============================================================

LANGUAGES: dict[str, LanguageConfig] = {
    "csharp": LanguageConfig(
        name="C#/Unity",
        extensions=[".cs"],
        semgrep_rulesets=["p/csharp"],
        custom_rules_file="unity-rules.yml",
        expertise=(
            "Unity lifecycle (Awake, Start, Update, FixedUpdate, LateUpdate, OnDestroy). "
            "Object pooling vs Instantiate/Destroy patterns. "
            "Main thread safety for Unity API calls. "
            "Garbage collection pressure in hot paths. "
            "MonoBehaviour and ScriptableObject best practices. "
            "Coroutine vs async/await patterns in Unity."
        ),
        prompt_extras=[
            "Unity performance anti-patterns",
            "Memory allocation in hot paths",
            "Threading safety issues",
            "Coroutine misuse (StartCoroutine on disabled objects)",
            "Missing null checks on Unity object references",
        ],
    ),
    "typescript": LanguageConfig(
        name="TypeScript",
        extensions=[".ts", ".tsx"],
        semgrep_rulesets=["p/typescript"],
        expertise=(
            "TypeScript strict mode and type safety. "
            "React component patterns (hooks, memoization, render optimization). "
            "Async/await error handling. Promise patterns. "
            "Module import organization. Null/undefined safety."
        ),
        prompt_extras=[
            "Type safety gaps and `any` casts",
            "Unhandled promise rejections",
            "`readFileSync`/`writeFileSync`/`execSync` inside async functions (blocks event loop)",
            "`Promise.all()` for bulk ops without `Promise.allSettled()` (silent partial failure)",
            "Read-modify-write without transaction/locking (race condition)",
            "Input validation BEFORE URL decoding (`%2e%2e` bypasses `..` check)",
            "Client-provided MIME types trusted without magic-byte validation",
            "Auth that returns early for unknown user (timing attack / username enumeration)",
            "Fastify/Express middleware that sends reply without early `return` (route still executes)",
            "Mixed auth patterns (session cookies vs stateless JWT vs localStorage) in same codebase",
            "RBAC checks inconsistent across routes for the same resource type",
        ],
    ),
    "javascript": LanguageConfig(
        name="JavaScript",
        extensions=[".js", ".jsx"],
        semgrep_rulesets=["p/javascript"],
        expertise=(
            "Modern ES2022+ patterns. React component patterns. "
            "Async/await and Promise handling. Closure and scope issues. "
            "Event loop understanding. Module system (ESM vs CJS)."
        ),
        prompt_extras=[
            "Prototype and closure pitfalls",
            "Unhandled async errors",
            "`Promise.all()` for bulk ops without `Promise.allSettled()`",
            "Sync I/O (readFileSync) in async context",
        ],
    ),
    "python": LanguageConfig(
        name="Python",
        extensions=[".py"],
        semgrep_rulesets=["p/python"],
        expertise=(
            "PEP 8 style and idioms. Type hints and mypy compatibility. "
            "Context managers and resource cleanup. "
            "Async/await patterns. Security pitfalls (injection, pickle). "
            "Performance (generators, list comprehensions)."
        ),
        prompt_extras=[
            "Type hint correctness",
            "Resource leaks (unclosed files, connections)",
            "Security issues (injection, unsafe deserialization, pickle)",
            "Mutable default arguments",
            "Exception swallowing (bare `except:` clauses)",
        ],
    ),
    "java": LanguageConfig(
        name="Java",
        extensions=[".java"],
        semgrep_rulesets=["p/java"],
        expertise=(
            "Java best practices and design patterns. "
            "Null safety (Optional usage). Resource management (try-with-resources). "
            "Concurrency and thread safety. Stream API usage."
        ),
        prompt_extras=[
            "Null pointer dereference without Optional",
            "Resource leaks (missing try-with-resources)",
            "Thread safety issues (shared mutable state)",
            "Checked exceptions swallowed silently",
        ],
    ),
    "go": LanguageConfig(
        name="Go",
        extensions=[".go"],
        semgrep_rulesets=["p/go"],
        expertise=(
            "Go idioms and effective Go patterns. "
            "Error handling (no swallowed errors). Goroutine and channel safety. "
            "defer/panic/recover patterns. Context propagation."
        ),
        prompt_extras=[
            "Swallowed errors (err assigned but not checked)",
            "Goroutine leaks (goroutine started without cancel/done signal)",
            "Race conditions on shared state without mutex",
            "Missing context propagation in API calls",
        ],
    ),
}


# ============================================================
#  YAML skill loader
# ============================================================

def _load_skill_yaml(path: Path) -> tuple[str, LanguageConfig] | None:
    """Load a skill YAML file. Returns (lang_key, config) or None on error."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not data or "name" not in data or "extensions" not in data:
            logger.warning(f"Skill {path.name}: missing required fields (name, extensions)")
            return None
        key = path.stem.lower().replace("-", "_").replace(" ", "_")
        return key, LanguageConfig(
            name=data["name"],
            extensions=data["extensions"],
            semgrep_rulesets=data.get("semgrep_rulesets", []),
            custom_rules_file=data.get("custom_rules"),
            expertise=data.get("expertise", ""),
            prompt_extras=data.get("prompt_extras", []),
        )
    except Exception as e:
        logger.warning(f"Skipping skill {path.name}: {e}")
        return None


def _load_skills_from_dir(skills_dir: Path) -> dict[str, LanguageConfig]:
    """Load all .yml files from a skills directory."""
    result = {}
    if not skills_dir.is_dir():
        return result
    for f in sorted(skills_dir.glob("*.yml")):
        loaded = _load_skill_yaml(f)
        if loaded:
            key, config = loaded
            result[key] = config
            logger.info(f"Loaded skill: {config.name} ({', '.join(config.extensions)})")
    return result


def get_languages() -> dict[str, LanguageConfig]:
    """Return merged LANGUAGES: built-ins + user skills + project skills.

    Loading order (later wins):
    1. Built-in LANGUAGES dict
    2. ~/.boomai/skills/*.yml  (user-level, applies to all projects)
    3. .boomai/skills/*.yml    (project-level, highest priority)
    """
    merged = dict(LANGUAGES)

    # User-level skills
    user_skills_dir = Path.home() / ".boomai" / "skills"
    merged.update(_load_skills_from_dir(user_skills_dir))

    # Project-level skills (cwd)
    project_skills_dir = Path.cwd() / ".boomai" / "skills"
    merged.update(_load_skills_from_dir(project_skills_dir))

    return merged


# ============================================================
#  Extension / language detection helpers
# ============================================================

def detect_languages(filenames: list[str]) -> list[str]:
    """Detect languages from a list of filenames. Returns sorted language keys."""
    langs = get_languages()
    ext_map: dict[str, str] = {}
    for lang_key, config in langs.items():
        for ext in config.extensions:
            ext_map[ext] = lang_key

    detected = set()
    for filename in filenames:
        _, ext = os.path.splitext(filename)
        if ext in ext_map:
            detected.add(ext_map[ext])
    return sorted(detected)


def get_reviewable_extensions() -> set[str]:
    """Return all file extensions BoomAI can review (built-ins + loaded skills)."""
    return {ext for config in get_languages().values() for ext in config.extensions}


def filter_reviewable_files(filenames: list[str]) -> list[str]:
    """Filter filenames to only those BoomAI can review."""
    exts = get_reviewable_extensions()
    return [f for f in filenames if os.path.splitext(f)[1] in exts]
