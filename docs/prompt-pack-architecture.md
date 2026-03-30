# Prompt Pack Architecture For BoomAI

## Purpose

BoomAI currently relies on a strong but mostly monolithic prompt for Gemini scan and patch generation. That approach helped the project get off the ground quickly, but it has clear limits:

- prompt growth increases cost and latency
- giant prompts increase malformed JSON risk on large scans
- the model receives many irrelevant guidelines for a given chunk
- it is hard to reason about which guidance actually improves findings
- there is no clean path toward prompt caching, telemetry, or future tuning

This document defines the next architecture step: a modular, versioned prompt-pack system that lets BoomAI inject only the most relevant domain guidance into each request.

The design intentionally separates three concerns:

1. knowledge storage
2. pack selection
3. prompt composition and eventual caching

## Goals

- Make BoomAI more domain-aware for C#, Unity, parsing, streams, save-data logic, and auto-fix safety.
- Avoid sending a giant static prompt on every request.
- Establish a stable prompt contract that can later plug into Gemini explicit caching.
- Keep the first implementation small and low-risk.
- Add telemetry so we can measure which packs are selected and whether they improve quality.

## Non-Goals

- This is not a fine-tuning system.
- This is not a full local result-cache system yet.
- This is not the final Gemini explicit cache implementation.
- This is not a replacement for chunking, retrieval, or issue-seed guidance.

## Core Idea

Instead of one prompt that tries to encode every rule BoomAI might ever need, we introduce reusable prompt packs.

A prompt pack is a versioned unit of domain knowledge. Each pack contains:

- what type of code it applies to
- what issues to look for
- what false positives to avoid
- what fix constraints matter

Examples:

- `core-csharp`
- `unity-lifecycle`
- `binary-parsing`
- `stream-io`
- `collections-nullability`
- `save-data-integrity`
- `autofix-safety`

For each request, BoomAI selects a small subset of packs and merges them into the prompt.

## Why This Is Better Than A Giant Prompt

### Better relevance

A chunk under `Assets/Scripts/API/` that contains `BinaryReader`, `ReadBytes`, `Seek`, and `Stream` should receive parsing and stream guidance, not Unity `MonoBehaviour` guidance.

### Better stability

Smaller prompts reduce JSON truncation risk and reduce the chance that the model focuses on the wrong instruction cluster.

### Better cost profile

If the stable knowledge prefix is modular and reusable, it becomes a strong candidate for Gemini explicit caching later.

### Better measurement

We can record:

- which packs were selected
- how often each pack is used
- whether a pack correlates with more useful findings
- whether a pack correlates with more fixable findings

## Architectural Layers

### Layer 1: Prompt Pack Registry

The registry stores all available packs in code as typed Python objects.

Responsibilities:

- define pack metadata
- define stage applicability (`scan`, `fix`, or both)
- expose pack lookup by id
- provide a stable version string for future cache keys

The registry is intentionally local and deterministic.

First implementation choice:

- define packs in Python instead of JSON/YAML

Reason:

- simpler packaging
- fewer runtime file-loading concerns
- no extra wheel/package-data complexity

### Layer 2: Pack Selector

The selector decides which packs are relevant for a request.

Inputs:

- file paths
- file contents
- issue seeds
- stage (`scan` or `fix`)

Signals:

- path-based heuristics
- content keyword heuristics
- issue-seed keyword heuristics

Selector output:

- ordered list of selected packs

Rules:

- always include `core-csharp`
- include only a small number of extra packs
- prefer precision over recall in the first version

### Layer 3: Prompt Composer

The composer converts selected packs into prompt text.

Responsibilities:

- render selected packs in a compact structured section
- keep base prompt instructions authoritative
- append pack guidance in a consistent format
- produce a stable text layout for future caching

The composer does not decide which packs apply. It only renders them.

## Pack Schema

The first version keeps the schema deliberately small.

Recommended fields:

- `id`
- `version`
- `title`
- `stages`
- `summary`
- `when_to_use`
- `review_focus`
- `fix_focus`
- `avoid`

## Initial Pack Set

### `core-csharp`

Always included.

Focus:

- nullability and reference safety
- collection and dictionary correctness
- off-by-one and bounds errors
- disposal and resource ownership
- exception safety
- mutation vs copy semantics

### `unity-lifecycle`

Use when code looks like Unity runtime code.

Focus:

- `Awake`/`Start`/`OnEnable`/`OnDisable`/`OnDestroy`
- event subscription cleanup
- `Update`/`FixedUpdate` hot-path allocations
- shared `ScriptableObject` mutation
- object lifetime mistakes

### `binary-parsing`

Use for file parsers, binary readers, offsets, buffers, lengths.

Focus:

- unchecked file-provided lengths
- offset and seek correctness
- EOF handling
- array allocation safety
- malformed-file resilience

### `stream-io`

Use for `Stream`, `FileStream`, `BinaryReader`, `BinaryWriter`, `Read`, `Write`.

Focus:

- partial reads
- dispose/close ownership
- flush semantics
- leaving streams open unexpectedly
- lifetime corruption

### `collections-nullability`

Use for `Dictionary`, `List`, `HashSet`, null checks, missing guards.

Focus:

- `TryGetValue`
- missing null/empty checks
- mutation during iteration
- wrong key assumptions

### `save-data-integrity`

Use for save files, serialization, persistence, records, indices.

Focus:

- truncation/corruption risks
- shallow copy bugs
- wrong record boundaries
- destructive mutation of parsed state

### `autofix-safety`

Use mainly in fix stage.

Focus:

- prefer smallest safe exact patch
- preserve semantics
- avoid speculative refactors
- avoid policy-level changes without clear local evidence

## Selection Heuristics

The first version should be explicit and heuristic-based, not learned.

### Base rule

Always include:

- `core-csharp`

### Path-based signals

Examples:

- path contains `Save/` -> `save-data-integrity`
- path contains `Assets/Scripts` or file contains `MonoBehaviour` -> `unity-lifecycle`
- file name ends with `File.cs` or path contains `API/` -> candidate for `binary-parsing` and `stream-io`

### Content-based signals

Examples:

- `BinaryReader`, `ReadBytes`, `Seek`, `Position`, `offset`, `recordCount` -> `binary-parsing`
- `Stream`, `FileStream`, `BinaryWriter`, `StreamReader`, `Dispose`, `Close`, `Flush` -> `stream-io`
- `Dictionary`, `TryGetValue`, `ContainsKey`, `null`, `Nullable` -> `collections-nullability`
- `Save`, `Serialize`, `Deserialize`, `RecordData`, `Write`, `Load`, `slot`, `saveGame` -> `save-data-integrity`

### Issue-seed signals

If static findings mention:

- EOF / bounds / malformed data -> `binary-parsing`
- partial reads / stream lifetime -> `stream-io`
- missing key / null reference -> `collections-nullability`
- truncation / corruption / wrong save index -> `save-data-integrity`

## Prompt Composition Strategy

The base prompt remains the root authority.

Prompt packs are appended as a structured domain guidance section.

Recommended layout:

1. base system prompt
2. selected prompt pack section
3. dynamic user payload

The pack section must be:

- compact
- deterministic
- stable enough for future caching

## Caching Design

### What exists today

BoomAI currently does not implement its own Gemini prompt cache or local response cache.

That means:

- the same request can be sent multiple times
- there is no local reuse keyed on request content
- there is no explicit Gemini `cachedContent` integration yet

### What this architecture enables next

Prompt packs are the right unit for explicit Gemini caching because they are:

- large enough to matter
- stable across many requests
- reused across runs

Future Gemini explicit cache keys should include:

- model name
- pack id
- pack version
- content hash

## Telemetry Requirements

The MVP should log selected pack ids in debug mode.

Examples:

- `scan packs: core-csharp, binary-parsing, stream-io`
- `fix packs: core-csharp, autofix-safety, stream-io`

The next stage should add:

- pack frequency
- per-pack finding volume
- per-pack fixable/non-fixable correlation
- cache-hit metrics

## Rollout Plan

### Phase 1: Modular prompt packs

Deliverables:

- prompt pack registry
- pack selector
- prompt composer
- scan-stage integration
- optional fix-stage integration
- debug telemetry for selected packs

No explicit Gemini caching yet.

### Phase 2: Gemini explicit cache

Deliverables:

- local cache metadata index
- cached pack creation/reuse
- cached token telemetry

### Phase 3: Smarter selection and evaluation

Deliverables:

- better heuristics
- pack effectiveness measurements
- automatic pruning of low-value packs

## Risks

### Over-selection

If we select too many packs, prompt bloat returns.

Mitigation:

- always include `core-csharp`
- cap extras aggressively

### Weak heuristics

Selector heuristics might choose the wrong pack or miss one.

Mitigation:

- keep the first pack set small
- log pack choices in debug mode
- refine using real BoomAI logs

### Conflicting guidance

Different packs could subtly push the model in different directions.

Mitigation:

- base prompt remains authoritative
- keep pack content concise and domain-specific

## Success Criteria

We should consider the architecture successful when:

- selected packs are relevant in most chunks
- prompt size stays controlled
- findings quality improves or stays stable
- fixable rate improves or stays stable
- debug logs clearly show what domain guidance was applied
- the system is ready for explicit Gemini caching without major refactor

## Recommended Immediate Next Steps

1. implement pack registry in Python
2. implement heuristic selector
3. append selected packs into scan and fix system prompts
4. log selected pack ids in debug mode
5. run a few real BoomAI scans and inspect:
   - chosen packs
   - findings quality
   - prompt size changes
6. only then add Gemini explicit cache integration
