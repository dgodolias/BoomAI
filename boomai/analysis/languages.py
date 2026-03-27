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
            "Object pool return-before-invoke: `pool.TryPush(this)` placed before `continuation.Invoke()` — another thread can pop and reconfigure the same object while the original continuation is still executing; always invoke the continuation before returning to the pool",
            "SynchronizationContext.CreateCopy() returning `this` instead of a new instance — all async operations that capture-and-restore context share one dispatch queue, causing cross-task contamination; CreateCopy() must return `new DerivedContext()` with its own isolated state",
            "Static fields (SpinLock, queues, counts) used as per-instance state in a SynchronizationContext or similar per-instance class create a hidden global singleton — all instances share one lock and one action queue, so callbacks posted to different context instances race each other and execute in the wrong context",
            "Catch-and-swallow inside error-reporting callbacks: bare `catch { }` or `catch (Exception) { }` wrapped around `unhandledExceptionCallback(ex)` or error-handler invocations silently discards failures inside the error handler itself — lock corruption, null refs, and handler bugs become invisible and undebuggable",
            "Pooled promise cancellation-path resource leak: `GetResult` skips `TryReturn()` when `cancelImmediately && token.IsCancellationRequested`, leaving the `CancellationTokenRegistration` undisposed and the object permanently leaked from the pool — always dispose registrations and return to pool in ALL exit paths, not just the success path",
            "CancellationTokenSource wrapped in a disposable that calls `Cancel()` but never `Dispose()` — `CancellationTokenSource` holds internal kernel handles on some runtimes; wrappers must call both `cts.Cancel()` and `cts.Dispose()` on cleanup to avoid native resource leaks",
            "Sequential `DisposeAsync` loop over multiple `IAsyncDisposable` items without per-item try-finally — if one `DisposeAsync()` throws, remaining items are never disposed and rented buffers (ArrayPool) are never returned; wrap each disposal in its own try-catch or aggregate exceptions",
            "Unchecked integer arithmetic on network/file-provided size values before cast or allocation — attacker-controlled count can overflow or wrap, causing massive allocation (OOM) or negative index (buffer corruption); always validate against a reasonable maximum before using the value",
            "Buffer/array capacity growth (e.g. `Math.Max(needed, buffer.Length * 2)`) without an explicit maximum cap — a single large message or malformed input triggers unbounded allocation that exhausts process memory",
            "Unbounded delta/change-tracking collection (`List<Change>`) that grows until an explicit `Clear()` call — if the clear is skipped (error path, timing issue, disabled component), the list balloons indefinitely causing GC pressure and memory leaks",
            "Serialization contract violation handled with soft warning instead of hard failure: `OnDeserialize` size mismatch logs a warning but continues execution — all subsequent fields/components deserialize from the wrong stream offset, producing silent data corruption",
            # --- Unity game patterns (audit-derived) ---
            "`Animator.SetBool/SetTrigger/SetFloat/SetInteger` called with string literals instead of cached `Animator.StringToHash` int hashes — Unity hashes the string on every call; in hot paths (movement, combat) this adds measurable CPU overhead per animated unit per frame; declare `private static readonly int hashName = Animator.StringToHash(\"Name\");` and pass the int",
            "`DontDestroyOnLoad(gameObject)` without checking for existing duplicates — when the initialization scene is reloaded (e.g. game restart from menu), a second persistent copy is created, causing double event handling, duplicate managers, and memory leaks; guard with `if (instance != null && instance != this) { Destroy(gameObject); return; }`",
            "`Physics.OverlapSphere/OverlapBox/Raycast` (allocating variants) used in FixedUpdate or per-frame code — each call allocates a new `Collider[]` array; use `Physics.OverlapSphereNonAlloc/OverlapBoxNonAlloc` with a pre-allocated buffer to eliminate per-frame GC pressure",
            "`GetComponent<T>()`/`GetComponentInParent<T>()`/`FindObjectOfType<T>()` called inside `Update`/`LateUpdate`/`FixedUpdate` or per-frame callbacks instead of caching the result in `Awake`/`Start` — each call is an O(n) hierarchy search that generates GC pressure; cache once and reuse",
            "ScriptableObject fields modified at runtime (outside Instantiate) — ScriptableObjects are shared assets; mutating them affects every reference in the project and, in the Editor, permanently saves the change to disk; always `Instantiate()` before mutation or use a separate runtime data class",
            "Empty MonoBehaviour callback (`Update()`, `FixedUpdate()`, `LateUpdate()`, `OnGUI()`) with no logic or only comments — Unity still dispatches the native-to-managed interop call every frame/tick, adding measurable overhead; remove the method entirely if unused",
            "Synchronous `SceneManager.LoadScene()` freezes the main thread until the scene is fully loaded, causing visible hitches and ANRs on mobile — use `SceneManager.LoadSceneAsync()` with a loading screen or progress callback",
            "`new WaitForSeconds(duration)` allocated inside a coroutine loop (`while` or `for`) — Unity creates a new heap object on every iteration; cache as `private static readonly WaitForSeconds wait = new WaitForSeconds(1f);` for fixed durations, or cache per-instance for variable ones",
            "`LayoutRebuilder.ForceRebuildLayoutImmediate()` called multiple times per frame or inside frequent callbacks (combat log, damage numbers) — each call triggers a synchronous layout pass; prefer `LayoutRebuilder.MarkLayoutForRebuild()` (deferred) or batch updates into a single end-of-frame rebuild",
            "`new StateObject()` allocated on every AI state transition in `Update`/`FixedUpdate` — with many AI units, constant heap allocations cause GC spikes; use cached/pooled state instances per unit (lightweight state classes can be reused since they hold no per-instance data)",
            "Active `Debug.Log()` with string concatenation or boxing inside Update/FixedUpdate/hot paths — each call allocates strings and boxes value types; remove or wrap in `#if UNITY_EDITOR` / `[Conditional(\"UNITY_EDITOR\")]` for production builds",
            "String concatenation (`+`) or `string.Format` inside Update/LateUpdate/FixedUpdate for UI text — allocates new strings every frame; also, setting `TextMeshPro.text` every frame triggers a mesh rebuild even when the value hasn't changed; cache the previous value and only update on change",
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
