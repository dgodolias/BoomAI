"""Language detection and configuration for C#/Unity code review."""

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
            "Event handler asymmetry: `+=` added in Register/Add method but `-=` missing in the paired Unregister/Remove method — causes double invocation after Stop+Start or scene reload cycles",
            "Collection pre-allocated from network-provided element count without bytes-available guard (`new List<T>(networkLength)`) — element-count limit allows multi-GB allocation if T is a large struct",
            "NetworkReader position desync: skipping a component's dirty bit without consuming its serialized bytes — subsequent components deserialize corrupt data",
            "`async void` in Unity lifecycle methods (Awake/Start/OnDestroy/OnEnable/OnDisable) — Unity gets control back immediately; any continuation that resumes after the object is destroyed accesses null/destroyed references and throws unhandled exceptions",
            "Unity `Invoke(nameof(Method), delay)` with an `async Task` method — Invoke discards the returned Task, so exceptions are silently swallowed, the async work runs with no awaiter, and there is no cancellation path; use a coroutine or store and await the Task instead",
            "Anonymous lambda subscribed to an external C# event without storing the delegate reference (`event += (a, b) => Method()`) — the anonymous delegate cannot be unsubscribed later; the subscription persists permanently even after the subscriber is destroyed, causing memory leaks and stale callbacks after scene reload",
            "`Stream.Read(buffer, 0, count)` return value ignored — `Read()` may return fewer bytes than requested (valid for file and network streams); without looping until all bytes are consumed or using `BinaryReader.ReadBytes()`, subsequent parsing silently operates on partial/corrupt data",
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
