"""Versioned prompt packs for BoomAI review and patch guidance."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptPack:
    id: str
    version: str
    title: str
    stages: frozenset[str]
    summary: str
    when_to_use: tuple[str, ...]
    review_focus: tuple[str, ...] = ()
    fix_focus: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()


PROMPT_PACKS: tuple[PromptPack, ...] = (
    PromptPack(
        id="core-csharp",
        version="1.0.0",
        title="Core C# Safety",
        stages=frozenset({"scan", "fix"}),
        summary=(
            "General C# review guidance for correctness, nullability, collection safety, "
            "resource ownership, and defensive coding."
        ),
        when_to_use=("Always include for C# code review and patch generation.",),
        review_focus=(
            "Check nullability, ownership, and missing defensive guards before dereference.",
            "Look for bounds errors, off-by-one mistakes, and unchecked indices or lengths.",
            "Validate collection and dictionary access assumptions; prefer safe lookup patterns.",
            "Flag mutation or copy semantics bugs, especially shallow-copy state corruption.",
            "Watch for disposal, exception-safety, and lifetime bugs around handles and streams.",
        ),
        fix_focus=(
            "Prefer small local patches that improve correctness without widening behavior.",
            "Add explicit guards only when local evidence clearly supports them.",
        ),
        avoid=(
            "Avoid style-only or naming-only findings unless they hide correctness risk.",
            "Avoid speculative architecture changes in the patch stage.",
        ),
    ),
    PromptPack(
        id="unity-lifecycle",
        version="1.0.0",
        title="Unity Lifecycle",
        stages=frozenset({"scan", "fix"}),
        summary=(
            "Unity-specific guidance for MonoBehaviour lifetime, event cleanup, hot paths, "
            "and scene or object lifecycle correctness."
        ),
        when_to_use=(
            "Use for MonoBehaviour, ScriptableObject, Update/FixedUpdate, Awake/Start, and event-heavy Unity code.",
        ),
        review_focus=(
            "Check Awake/Start/OnEnable/OnDisable/OnDestroy ordering and cleanup correctness.",
            "Look for event subscriptions without matching unsubscription on destroy or disable.",
            "Flag hot-path allocations or repeated expensive calls in Update and FixedUpdate.",
            "Watch for shared runtime mutation of ScriptableObject or static state.",
        ),
        fix_focus=("Prefer safe lifecycle cleanup and local guard fixes over broader refactors.",),
        avoid=("Avoid reporting generic Unity style nits without concrete runtime impact.",),
    ),
    PromptPack(
        id="binary-parsing",
        version="1.0.0",
        title="Binary Parsing",
        stages=frozenset({"scan", "fix"}),
        summary=(
            "Defensive review guidance for offsets, lengths, record counts, array allocation, "
            "and malformed-file resilience."
        ),
        when_to_use=(
            "Use for BinaryReader, record or offset parsing, byte buffers, seek math, and file-format readers.",
        ),
        review_focus=(
            "Validate file-provided lengths, offsets, counts, and indices before allocation or access.",
            "Check seek origin and offset arithmetic carefully.",
            "Look for malformed-file paths that cause OOM, IndexOutOfRange, or infinite loops.",
            "Verify EOF handling and terminator-search loops.",
        ),
        fix_focus=(
            "Prefer explicit bounds and EOF guards near the failing read or allocation.",
            "Keep parser fixes surgical and format-preserving.",
        ),
        avoid=("Avoid speculative format changes or broad parser rewrites.",),
    ),
    PromptPack(
        id="stream-io",
        version="1.0.0",
        title="Stream And File I/O",
        stages=frozenset({"scan", "fix"}),
        summary=(
            "Guidance for Stream, FileStream, BinaryReader, BinaryWriter lifetime, partial reads, "
            "flush semantics, and ownership correctness."
        ),
        when_to_use=(
            "Use for Stream, FileStream, StreamReader, StreamWriter, BinaryReader, BinaryWriter, Read, Write, Close, Dispose, or Flush.",
        ),
        review_focus=(
            "Check whether reads assume full-length completion when APIs may return partial data.",
            "Validate close or dispose ownership and exception-safe cleanup.",
            "Look for resource leaks or corrupted lifetime when streams are nulled, replaced, or closed incorrectly.",
            "Check flush and close ordering and whether writer operations can corrupt persistence.",
        ),
        fix_focus=("Prefer local read loops, using or dispose safety, and ownership-preserving changes.",),
        avoid=("Avoid refactoring stream abstractions unless the bug is truly local and obvious.",),
    ),
    PromptPack(
        id="collections-nullability",
        version="1.0.0",
        title="Collections And Nullability",
        stages=frozenset({"scan", "fix"}),
        summary=(
            "Guidance for dictionary or list access, null checks, empty checks, and safe collection assumptions."
        ),
        when_to_use=(
            "Use for Dictionary, List, HashSet, array access, TryGetValue, ContainsKey, null, and empty-check-heavy code.",
        ),
        review_focus=(
            "Check dictionary lookups, missing-key assumptions, and safe fallback behavior.",
            "Look for null or empty collection access before indexing or iteration.",
            "Watch for mutation-during-iteration and stale references.",
        ),
        fix_focus=("Prefer precise guard clauses or TryGetValue-style replacements when behavior is locally clear.",),
        avoid=("Avoid over-reporting defensive-null style issues that do not change correctness.",),
    ),
    PromptPack(
        id="save-data-integrity",
        version="1.0.0",
        title="Save Data Integrity",
        stages=frozenset({"scan", "fix"}),
        summary=(
            "Guidance for serialization, persistence, record boundaries, destructive mutation, "
            "and save corruption risks."
        ),
        when_to_use=(
            "Use for save or load code, persistence records, serialization, deserialization, and record trees.",
        ),
        review_focus=(
            "Check truncation, corruption, boundary handling, and off-by-one record traversal.",
            "Look for destructive mutation of parsed or cached save state.",
            "Validate array copy lengths, indices, and stream boundaries against actual record sizes.",
            "Watch for shallow-copy bugs that alias mutable persistence state.",
        ),
        fix_focus=("Prefer integrity-preserving local fixes that prevent corruption or boundary mistakes.",),
        avoid=("Avoid redesigning the persistence format in the patch stage.",),
    ),
    PromptPack(
        id="autofix-safety",
        version="1.0.0",
        title="Autofix Safety",
        stages=frozenset({"fix"}),
        summary="Patch-stage safety guidance for minimal, exact, semantics-preserving edits.",
        when_to_use=("Use for all patch-generation requests.",),
        fix_focus=(
            "Prefer the smallest exact edit that clearly resolves the reported issue.",
            "Preserve existing structure, indentation, and semantics whenever possible.",
            "If safe local evidence is insufficient, avoid speculative rewrites.",
        ),
        avoid=(
            "Do not widen behavior or refactor unrelated code.",
            "Do not invent helper APIs or large abstractions for a local fix.",
        ),
    ),
)
