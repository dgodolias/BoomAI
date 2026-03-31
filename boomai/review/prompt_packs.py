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
        version="1.1.0",
        title="Core C# Safety",
        stages=frozenset({"scan", "fix"}),
        summary=(
            "General C# review guidance for correctness, nullability, collection safety, "
            "copy semantics, overflow-sensitive arithmetic, and resource ownership."
        ),
        when_to_use=(
            "Always include for C# code review and patch generation.",
            "Acts as the base pack when no narrower domain pack fully explains the failure mode.",
        ),
        review_focus=(
            "Check nullability, ownership, and missing defensive guards before dereference.",
            "Look for bounds errors, off-by-one mistakes, and unchecked indices or lengths.",
            "Validate collection and dictionary access assumptions; prefer safe lookup patterns.",
            "Flag mutation or copy semantics bugs, especially shallow-copy state corruption.",
            "Watch for disposal, exception-safety, and lifetime bugs around handles and streams.",
            "Treat value-type copies carefully when structs contain arrays or other reference-type fields.",
            "Pay attention to integer arithmetic that can overflow silently in default unchecked contexts.",
            "Inspect code paths that catch broad Exception and then continue, because this can hide data corruption or desync.",
            "Check whether helper methods return null, empty, or partial data that callers assume is complete.",
            "Prefer concrete runtime-risk findings over style findings unless style hides a correctness bug.",
            "Treat duplicated state, aliasing, and mutable shared fields as correctness risks, not just code quality concerns.",
        ),
        fix_focus=(
            "Prefer small local patches that improve correctness without widening behavior.",
            "Add explicit guards only when local evidence clearly supports them.",
            "Prefer exact, semantics-preserving fixes over speculative cleanup or redesign.",
            "When adding guards, choose failure behavior that matches nearby code instead of inventing a new policy.",
            "Prefer local validation before allocation, indexing, copy, or seek operations.",
            "If a fix changes copy semantics, clone only the specific mutable field that aliases shared state.",
        ),
        avoid=(
            "Avoid style-only or naming-only findings unless they hide correctness risk.",
            "Avoid speculative architecture changes in the patch stage.",
            "Avoid replacing clear local code with helper abstractions just to satisfy a finding.",
            "Avoid broad try/catch wrappers that hide the original failure mode.",
        ),
    ),
    PromptPack(
        id="unity-lifecycle",
        version="1.1.0",
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
            "Treat OnEnable/OnDisable symmetry as important because disable also happens on destroy, scene unload, and domain reload.",
            "Prefer cached component and object references over repeated searches in hot paths.",
            "Treat per-frame GetComponent, FindObjectOfType, Camera.main, and similar lookups as hot-path suspects when repeated.",
            "Look for allocations in frequently-called methods that will amplify GC pressure on mobile or busy scenes.",
            "Check whether runtime state is initialized in one lifecycle method but consumed earlier in another.",
            "Watch for Unity API usage from the wrong context or assumptions that objects remain valid after disable or destroy.",
        ),
        fix_focus=(
            "Prefer safe lifecycle cleanup and local guard fixes over broader refactors.",
            "Cache repeated lookups only when local evidence shows a hot-path call site.",
            "Prefer symmetric subscribe/unsubscribe fixes over redesigning the event flow.",
            "Keep lifecycle fixes local to the component unless cross-object ownership is already explicit in the code.",
        ),
        avoid=(
            "Avoid reporting generic Unity style nits without concrete runtime impact.",
            "Avoid performance advice on cold paths unless there is clear evidence the call site is hot.",
        ),
    ),
    PromptPack(
        id="binary-parsing",
        version="1.1.0",
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
            "Treat width*height, count*stride, offset+length, and similar arithmetic as overflow risks before allocation or seek.",
            "Do not assume ReadBytes returns the requested length; truncated input must be handled explicitly.",
            "Validate stream position changes against remaining length, not just total length.",
            "Check loop bounds driven by file headers, frame counts, record counts, and terminator scans.",
            "Inspect negative values, signed-to-unsigned casts, and unchecked casts from long to int before allocation or seek.",
            "Watch for recursive or linked-list style parsing without cycle detection or forward-progress guarantees.",
            "Treat fixed-size temporary buffers as overflow risks when file complexity can exceed expected limits.",
            "Check whether parser recovery paths leave the stream desynchronized for subsequent reads.",
        ),
        fix_focus=(
            "Prefer explicit bounds and EOF guards near the failing read or allocation.",
            "Keep parser fixes surgical and format-preserving.",
            "Prefer checked arithmetic or explicit upper-bound validation before allocation math.",
            "When a read can be partial, validate the returned length immediately rather than relying on downstream failures.",
            "Prefer using remaining-bytes validation over ad hoc total-length checks when the stream has already advanced.",
            "If malformed input cannot be handled safely, fail closed locally instead of guessing a continuation path.",
        ),
        avoid=(
            "Avoid speculative format changes or broad parser rewrites.",
            "Avoid inventing new file-format assumptions not justified by local parsing code.",
        ),
    ),
    PromptPack(
        id="stream-io",
        version="1.1.0",
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
            "Watch for BinaryReader/BinaryWriter wrappers that accidentally close a shared underlying stream.",
            "Prefer using/dispose patterns that still preserve ownership when leave-open semantics are intended.",
            "Treat manual Close calls in the middle of composite operations as ownership red flags.",
            "Inspect exception paths to see whether handles leak when a constructor, read, or write fails early.",
            "Check whether helper methods return reader or writer wrappers without documenting who disposes them.",
            "Watch for MemoryStream, BinaryReader, and BinaryWriter allocations in hot code paths that could be pooled or reused.",
        ),
        fix_focus=(
            "Prefer local read loops, using or dispose safety, and ownership-preserving changes.",
            "When wrapping a shared stream, avoid fixes that silently transfer or break ownership.",
            "Prefer using blocks or try/finally when ownership is local and unambiguous.",
            "If the underlying stream must stay open, preserve that contract explicitly instead of relying on incidental behavior.",
        ),
        avoid=(
            "Avoid refactoring stream abstractions unless the bug is truly local and obvious.",
            "Avoid introducing double-dispose patterns unless the owning contract is clearly idempotent.",
        ),
    ),
    PromptPack(
        id="collections-nullability",
        version="1.1.0",
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
            "Prefer TryGetValue-style access when missing keys are expected or plausible.",
            "Validate Count/Length before direct indexing, especially with tokens[0], array[1], or similar patterns.",
            "Treat split results, token arrays, and parser-produced lists as untrusted until length is checked.",
            "Check whether a collection snapshot is needed before iteration when the loop body mutates related state.",
            "Look for ContainsKey plus indexer patterns that can be simplified into one safe lookup.",
            "Flag collection assumptions hidden behind helper methods that return null, empty, or resized arrays.",
        ),
        fix_focus=(
            "Prefer precise guard clauses or TryGetValue-style replacements when behavior is locally clear.",
            "If a collection is modified during enumeration, prefer iterating a stable snapshot only when that preserves intent.",
            "Prefer the narrowest guard that prevents invalid indexing without swallowing valid states.",
            "When a missing key is legitimate, preserve caller behavior with a clear fallback instead of throwing later.",
        ),
        avoid=(
            "Avoid over-reporting defensive-null style issues that do not change correctness.",
            "Avoid replacing direct indexing with broad defensive code if the surrounding invariant is clearly guaranteed.",
        ),
    ),
    PromptPack(
        id="save-data-integrity",
        version="1.1.0",
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
            "Be suspicious of structs or records that copy arrays, lists, or parsed sub-objects by value.",
            "Validate record lengths against remaining bytes before seeking, allocating, or copying.",
            "Check duplicate keys or record identifiers in reconstruction logic to avoid corruption on malformed saves.",
            "Inspect record-tree traversal and child-linking logic for cycles, duplicate IDs, and skipped terminal records.",
            "Treat save methods that only partially persist state as corruption risks, not just incomplete features.",
            "Check hardcoded offsets against remaining bytes, record length, and version-sensitive layout assumptions.",
            "Watch for cached parsed data being mutated in-place and then reused across loads or records.",
        ),
        fix_focus=(
            "Prefer integrity-preserving local fixes that prevent corruption or boundary mistakes.",
            "Prefer deep-copy or defensive-clone fixes only for the specific mutable field that aliases state.",
            "When validating record sizes, use remaining bytes and actual record contracts instead of magic numbers alone.",
            "Prefer fixes that fail safely on malformed save data over fixes that continue with a desynchronized stream.",
        ),
        avoid=(
            "Avoid redesigning the persistence format in the patch stage.",
            "Avoid save-format migrations or schema changes when the bug can be fixed with local validation or copy isolation.",
        ),
    ),
    PromptPack(
        id="copy-aliasing-and-mutability",
        version="1.0.0",
        title="Copy Aliasing And Mutability",
        stages=frozenset({"scan", "fix"}),
        summary=(
            "Guidance for shallow-copy bugs, aliased mutable state, struct copies with reference fields, "
            "and accidental mutation of cached or shared data."
        ),
        when_to_use=(
            "Use for Clone, CopyTo, MemberwiseClone, copy constructors, cached arrays, mutable parsed data, and struct-heavy state containers.",
            "Especially useful when code copies records, parser outputs, or cached objects that still contain arrays, lists, or nested mutable state.",
        ),
        review_focus=(
            "Treat struct copies as shallow for any embedded arrays, lists, strings builders, or reference-type fields.",
            "Look for Clone or CopyTo logic that duplicates the outer object but reuses mutable inner arrays or lists.",
            "Watch for cached parsed data being filtered, rewritten, or resized in place and then reused later.",
            "Check whether helper methods return direct references to internal buffers that callers then mutate.",
            "Inspect list and array assignments in copy paths for shared-state corruption, not just stylistic duplication.",
            "Be suspicious of state containers that look immutable but expose mutable fields or arrays by reference.",
        ),
        fix_focus=(
            "Prefer cloning only the specific mutable field that aliases state instead of rewriting the whole type.",
            "When a copy path must isolate state, preserve existing semantics for immutable fields and duplicate only mutable collections or arrays.",
            "Prefer local defensive copies at mutation points when redesigning ownership would be too invasive.",
        ),
        avoid=(
            "Avoid broad deep-clone rewrites across entire object graphs unless the local bug truly requires it.",
            "Avoid converting structs to classes or redesigning large data models in the patch stage unless the finding cannot be fixed locally.",
        ),
    ),
    PromptPack(
        id="thread-safety-and-shared-state",
        version="1.0.0",
        title="Thread Safety And Shared State",
        stages=frozenset({"scan", "fix"}),
        summary=(
            "Guidance for static mutable state, shared caches, PRNG usage, non-thread-safe collections, "
            "and race-prone access patterns."
        ),
        when_to_use=(
            "Use for static fields, shared dictionaries, mutable caches, Random or seed management, locks, interlocked code, and concurrent access paths.",
            "Especially useful when parser caches or global registries may be touched from multiple threads.",
        ),
        review_focus=(
            "Treat System.Collections.Generic collections as non-thread-safe when multiple threads can mutate them.",
            "Look for shared static dictionaries, lists, caches, or registries updated without synchronization.",
            "Watch for global pseudo-random state that is reset or mutated in helpers and can corrupt unrelated callers.",
            "Check for lock-free assumptions that are not backed by immutable data, Concurrent collections, or Interlocked usage.",
            "Inspect read-then-write sequences on shared state for races even when individual operations look harmless.",
            "Prefer Concurrent collections or explicit synchronization when concurrent mutation is clearly possible.",
        ),
        fix_focus=(
            "Prefer the smallest synchronization or isolation fix that protects the shared mutable state in question.",
            "If the code only needs thread-safe random generation, prefer APIs designed for concurrency instead of homegrown locking where practical.",
            "When concurrency is only potential, avoid invasive redesign and favor targeted guards or ownership isolation.",
        ),
        avoid=(
            "Avoid speculative locking around large code regions that could deadlock or change behavior without clear evidence.",
            "Avoid introducing concurrency primitives casually when a simpler ownership or immutability fix solves the local bug.",
        ),
    ),
    PromptPack(
        id="unsafe-buffer-and-rle-decode",
        version="1.0.0",
        title="Unsafe Buffer And RLE Decode",
        stages=frozenset({"scan", "fix"}),
        summary=(
            "Guidance for byte-buffer copying, image or frame decode loops, RLE logic, palette handling, "
            "and src/dst position safety."
        ),
        when_to_use=(
            "Use for RLE, frame buffers, palettes, srcPos/dstPos math, Array.Copy, Buffer.BlockCopy, decode loops, and image or video format readers.",
            "Especially useful for IMG, GFX, FLC, CFA, PAK, VID, SKY, and similar decode-heavy file readers.",
        ),
        review_focus=(
            "Check src and dst offsets before every copy or write into frame, palette, or extraction buffers.",
            "Treat run-length counts, probe lengths, and opcode-driven loops as untrusted until bounded against remaining input and output.",
            "Look for off-by-one writes around pos + 1, row boundaries, width*height frame extents, and final-pixel writes.",
            "Validate Array.Copy and Buffer.BlockCopy lengths against both source and destination remaining capacity.",
            "Inspect decode loops for forward-progress guarantees so malformed input cannot spin forever.",
            "Watch for palette and frame-buffer indexing that assumes a minimum decoded width, height, or color count.",
        ),
        fix_focus=(
            "Prefer precise boundary checks immediately around copy or write operations.",
            "Keep decode fixes format-preserving and local to the failing loop or copy site.",
            "When malformed data would exceed output bounds, fail safely instead of truncating in a way that hides corruption.",
        ),
        avoid=(
            "Avoid rewriting the whole decoder when the issue is a local bounds or count validation bug.",
            "Avoid changing byte-order or format interpretation unless the existing code already proves that assumption is wrong.",
        ),
    ),
    PromptPack(
        id="autofix-safety",
        version="1.1.0",
        title="Autofix Safety",
        stages=frozenset({"fix"}),
        summary="Patch-stage safety guidance for minimal, exact, semantics-preserving edits.",
        when_to_use=(
            "Use for all patch-generation requests.",
            "Acts as the patch-stage brake pedal when the domain packs suggest risky but plausible rewrites.",
        ),
        fix_focus=(
            "Prefer the smallest exact edit that clearly resolves the reported issue.",
            "Preserve existing structure, indentation, and semantics whenever possible.",
            "If safe local evidence is insufficient, avoid speculative rewrites.",
            "Prefer one local patch over multiple coordinated changes unless the finding cannot be fixed otherwise.",
            "Keep old_code exact and uniquely identifiable to reduce bad patch placement.",
            "When a fix cannot be proven safe from local context, return an empty patch instead of guessing.",
            "Favor fixes that are easy for a human reviewer to audit line-by-line.",
        ),
        avoid=(
            "Do not widen behavior or refactor unrelated code.",
            "Do not invent helper APIs or large abstractions for a local fix.",
            "Do not silently change ownership, threading model, serialization format, or lifecycle semantics unless the finding requires it.",
            "Do not convert a precise bug fix into a broad cleanup patch.",
        ),
    ),
)
