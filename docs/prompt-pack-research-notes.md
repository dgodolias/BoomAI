# Prompt Pack Research Notes

This note records the external sources used to enrich BoomAI's prompt packs so the guidance is not based only on model priors.

## Scope

The research focused on the recurring issue families already visible in BoomAI scan logs:

- binary parsing and malformed-input resilience
- stream and reader or writer ownership
- collection lookup and mutation safety
- value-type copy semantics with embedded reference fields
- checked arithmetic and overflow-sensitive allocation math
- Unity lifecycle and hot-path behavior

## Source-Backed Principles

### Binary parsing and partial reads

- `Stream.Read(...)` is not guaranteed to fill the requested buffer; callers must use the returned count and handle short reads.
- `BinaryReader.ReadBytes(count)` can return fewer bytes than requested at end-of-stream.
- Allocation and seek math around `width * height`, `count * stride`, and `offset + length` should be treated as overflow-sensitive before allocating or repositioning the stream.

These principles directly informed stronger guidance in `binary-parsing` and `stream-io`.

### Dispose and ownership

- The .NET dispose guidance emphasizes explicit ownership and reliable cleanup paths.
- Reader or writer wrappers can accidentally close the underlying stream, so fixes must preserve ownership when the stream is shared.

These principles informed the `stream-io` pack's emphasis on `using`, ownership-preserving fixes, and avoiding accidental close or dispose bugs.

### Collections and safe lookup

- `Dictionary<TKey, TValue>.TryGetValue(...)` is the standard safe lookup path when key absence is plausible.
- Enumerator invalidation and mutation-during-iteration are common correctness hazards in C# collection code.

These principles informed stronger `collections-nullability` guidance around guard clauses, safe lookups, and mutation-during-iteration.

### Struct and copy semantics

- C# structs are value types, but their fields can still reference heap objects; copying the struct copies those references, not deep clones.
- This makes structs with arrays, lists, or mutable sub-objects especially risky in parser and save-data code.

These principles informed `core-csharp` and `save-data-integrity`.

### Overflow-sensitive arithmetic

- In C#, integer overflow is context-sensitive and unchecked arithmetic can silently wrap.
- Parser code that uses file-derived sizes for buffer creation or seeking needs explicit validation or checked math.

These principles informed `core-csharp`, `binary-parsing`, and `save-data-integrity`.

### Unity lifecycle and hot paths

- Unity documents that `OnDisable` runs in more cases than many teams remember, including destroy, scene unload, and domain reload.
- Unity also recommends avoiding repeated expensive searches in hot paths and caching references where appropriate.

These principles informed `unity-lifecycle`.

## Sources

Primary and official sources:

1. Microsoft Learn, `Stream.Read` API docs  
   https://learn.microsoft.com/en-us/dotnet/api/system.io.stream.read?view=net-9.0

2. Microsoft Learn, `BinaryReader.ReadBytes` API docs  
   https://learn.microsoft.com/en-us/dotnet/api/system.io.binaryreader.readbytes?view=net-9.0

3. Microsoft Learn, Implement a Dispose method  
   https://learn.microsoft.com/en-us/dotnet/standard/garbage-collection/implementing-dispose

4. Microsoft Learn, `Dictionary<TKey,TValue>.TryGetValue` API docs  
   https://learn.microsoft.com/en-us/dotnet/api/system.collections.generic.dictionary-2.trygetvalue?view=net-9.0

5. Microsoft Learn, C# `struct` reference  
   https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/builtin-types/struct

6. Microsoft Learn, `checked` and `unchecked` statements  
   https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/statements/checked-and-unchecked

7. Unity Manual, Event function execution order  
   https://docs.unity3d.com/Manual/execution-order.html

8. Unity Scripting API, `MonoBehaviour.OnEnable`  
   https://docs.unity3d.com/ScriptReference/MonoBehaviour.OnEnable.html

9. Unity Scripting API, `MonoBehaviour.OnDisable`  
   https://docs.unity3d.com/ScriptReference/MonoBehaviour.OnDisable.html

10. Unity Manual, Mobile Optimization: practical scripting optimizations  
    https://docs.unity3d.com/Manual/MobileOptimizationPracticalScriptingOptimizations.html

Book and long-form background references:

11. Joseph Albahari, *C# in a Nutshell* book page and reference material  
    https://www.oreilly.com/library/view/c-in-a/0596001819/re180.html

12. Jeffrey Richter, *CLR via C#*, Microsoft Press Store  
    https://www.microsoftpressstore.com/store/clr-via-c-sharp-9780735668768

Practitioner references used only as secondary support:

13. Stack Overflow discussion on collection mutation during enumeration  
    https://stackoverflow.com/questions/5762494/invalidoperationexception-collection-was-modified-although-locking-the-collec

14. Stack Overflow discussion on `OnDisable` unsubscribe pitfalls in Unity  
    https://stackoverflow.com/questions/58302008/problem-with-event-unsubscribing-ondisable

## Notes

- Official documentation was weighted above forums.
- Book references were used to sanity-check broader language and runtime guidance, while concrete pack wording was anchored primarily to official docs.
- These notes are intended to justify pack content, not to freeze it; if BoomAI logs reveal new recurring issue families, the same research-first approach should be repeated before adding new packs.
