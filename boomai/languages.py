"""Language detection and configuration for multi-language code review."""

import os
from dataclasses import dataclass


@dataclass
class LanguageConfig:
    name: str
    extensions: list[str]
    semgrep_rulesets: list[str]
    custom_rules_file: str | None = None
    expertise: str = ""


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
    ),
}

# Reverse map: extension -> language key
_EXT_TO_LANG: dict[str, str] = {}
for _lang_key, _config in LANGUAGES.items():
    for _ext in _config.extensions:
        _EXT_TO_LANG[_ext] = _lang_key


def detect_languages(filenames: list[str]) -> list[str]:
    """Detect languages from a list of filenames. Returns sorted language keys."""
    detected = set()
    for filename in filenames:
        _, ext = os.path.splitext(filename)
        if ext in _EXT_TO_LANG:
            detected.add(_EXT_TO_LANG[ext])
    return sorted(detected)


def get_reviewable_extensions() -> set[str]:
    """Return all file extensions BoomAI can review."""
    exts = set()
    for config in LANGUAGES.values():
        exts.update(config.extensions)
    return exts


def filter_reviewable_files(filenames: list[str]) -> list[str]:
    """Filter filenames to only those BoomAI can review."""
    exts = get_reviewable_extensions()
    return [f for f in filenames if os.path.splitext(f)[1] in exts]
