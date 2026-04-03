from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from ..core.models import IssueSeed, ReviewComment

_WORD_RE = re.compile(r"[a-z_][a-z0-9_]{2,}")
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_INDEX_ACCESS_RE = re.compile(r"\b\w+\s*\[\s*\d+\s*\]")

_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "will", "into", "while",
    "used", "when", "where", "have", "has", "had", "can", "could", "should", "would",
    "null", "check", "checks", "missing", "inside", "outside", "line", "lines", "file",
    "files", "code", "data", "object", "objects", "value", "values", "array", "arrays",
}

_PATH_HINTS: dict[str, tuple[str, ...]] = {
    "unity-lifecycle": ("assets/scripts", "/editor/", "packages/com.unity", "projectsettings"),
    "binary-parsing": ("/api/", "/formats/", "/reader/", "mapsfile.cs", "bsafile.cs", "texturefile.cs"),
    "stream-io": ("fileproxy.cs", "stream", "reader", "writer"),
    "collections-nullability": ("dictionary", "list", "set"),
    "save-data-integrity": ("/save/", "savegames", "savetree", "characterrecord"),
    "copy-aliasing-and-mutability": ("/cache/", "/record", "/model", "/data/", "copy", "clone"),
    "thread-safety-and-shared-state": ("thread", "cache", "manager", "singleton"),
    "unsafe-buffer-and-rle-decode": ("gfxfile.cs", "flcfile.cs", "cfafile.cs", "imgfile.cs", "vidfile.cs"),
}

_TOKEN_HINTS: dict[str, tuple[str, ...]] = {
    "unity-lifecycle": (
        "monobehaviour", "scriptableobject", "awake", "start", "update", "fixedupdate",
        "lateupdate", "ondestroy", "onenable", "ondisable", "getcomponent", "camera",
        "coroutine", "dontdestroyonload", "listener", "gameobject",
    ),
    "binary-parsing": (
        "binaryreader", "readbytes", "seek", "offset", "recordcount", "position",
        "basestream", "overflowexception", "framecount", "eof", "truncated", "malformed",
    ),
    "stream-io": (
        "stream", "filestream", "streamreader", "streamwriter", "binarywriter", "dispose",
        "close", "flush", "read", "write", "leaveopen", "leave", "handle",
    ),
    "collections-nullability": (
        "dictionary", "trygetvalue", "containskey", "hashset", "nullreference",
        "keynotfound", "split", "tokens", "effectsplit", "enumeration",
    ),
    "save-data-integrity": (
        "serialize", "deserialize", "savegame", "recorddata", "streamlength", "recordid",
        "truncate", "corrupt", "clone", "copyto", "parseddata", "shallow",
    ),
    "copy-aliasing-and-mutability": (
        "clone", "copyto", "memberwiseclone", "shallow", "parseddata", "cached",
        "shared", "mutable", "alias", "reference", "defensive",
    ),
    "thread-safety-and-shared-state": (
        "static", "lock", "concurrentdictionary", "interlocked", "volatile", "thread",
        "task", "random", "seed", "shared", "race", "cache",
    ),
    "unsafe-buffer-and-rle-decode": (
        "rle", "framebuffer", "palette", "dstpos", "srcpos", "rowpos",
        "array", "copy", "decode", "probe", "framecount", "buffer",
    ),
}

_CALL_HINTS: dict[str, tuple[str, ...]] = {
    "unity-lifecycle": (
        "Awake", "Start", "Update", "FixedUpdate", "LateUpdate", "OnEnable", "OnDisable",
        "OnDestroy", "GetComponent", "FindObjectOfType", "StartCoroutine",
    ),
    "binary-parsing": ("ReadBytes", "ReadByte", "Seek", "Read", "ArrayCopy", "BlockCopy"),
    "stream-io": ("Dispose", "Close", "Flush", "Read", "Write"),
    "collections-nullability": ("TryGetValue", "ContainsKey", "Split"),
    "copy-aliasing-and-mutability": ("Clone", "CopyTo", "MemberwiseClone"),
    "thread-safety-and-shared-state": ("lock", "CompareExchange", "Increment"),
}

_CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "binary-parsing": ("binary", "buffer", "bounds", "rle", "decode", "allocation"),
    "stream-io": ("stream", "dispose", "close", "reader", "writer", "partial read"),
    "collections-nullability": ("null", "dictionary", "index", "key", "split", "tokens"),
    "save-data-integrity": ("save", "record", "serialize", "deserialize", "corruption"),
    "copy-aliasing-and-mutability": ("alias", "clone", "copy", "shallow", "cached"),
    "thread-safety-and-shared-state": ("thread", "race", "shared state", "random", "seed"),
    "unsafe-buffer-and-rle-decode": ("rle", "framebuffer", "palette", "srcpos", "dstpos"),
    "unity-lifecycle": ("unity", "monobehaviour", "lifecycle", "update", "coroutine"),
}


@dataclass(frozen=True, slots=True)
class PackSignals:
    path_tokens: Counter[str]
    content_tokens: Counter[str]
    call_tokens: Counter[str]
    categories: Counter[str]
    source_tokens: Counter[str]
    file_count: int


def _normalize_text(value: str) -> str:
    return value.lower().replace("\\", "/")


def _tokenize_words(text: str) -> list[str]:
    return [token for token in _WORD_RE.findall(text.lower()) if token not in _STOPWORDS]


def _extract_content_tokens(content: str) -> Counter[str]:
    limited = content[:80_000]
    words = Counter(_tokenize_words(limited))
    call_names = Counter(name.lower() for name in _CALL_RE.findall(limited))
    if _INDEX_ACCESS_RE.search(limited):
        words["indexed-access"] += 1
    return words + call_names


def _extract_call_tokens(content: str) -> Counter[str]:
    calls = Counter()
    for name in _CALL_RE.findall(content[:80_000]):
        calls[name] += 1
    return calls


def _seed_text(seed: IssueSeed | ReviewComment) -> str:
    return " ".join(
        part
        for part in (
            getattr(seed, "file", ""),
            getattr(seed, "category", "") or "",
            getattr(seed, "rule_id", "") or "",
            getattr(seed, "body", getattr(seed, "message", "")) or "",
        )
        if part
    )


def _collect_signals(
    file_contents: list[tuple[str, str]],
    issue_seeds: list[IssueSeed | ReviewComment] | None = None,
) -> PackSignals:
    path_tokens: Counter[str] = Counter()
    content_tokens: Counter[str] = Counter()
    call_tokens: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    source_tokens: Counter[str] = Counter()

    for path, content in file_contents:
        normalized_path = _normalize_text(path)
        path_tokens.update(_tokenize_words(normalized_path))
        path_tokens.update(segment for segment in normalized_path.split("/") if segment)
        content_tokens.update(_extract_content_tokens(content))
        call_tokens.update(_extract_call_tokens(content))

    for seed in issue_seeds or []:
        categories[(getattr(seed, "category", "") or "").lower()] += 1
        source_tokens[(getattr(seed, "source", "") or "").lower()] += 1
        content_tokens.update(_tokenize_words(_seed_text(seed)))

    return PackSignals(
        path_tokens=path_tokens,
        content_tokens=content_tokens,
        call_tokens=call_tokens,
        categories=categories,
        source_tokens=source_tokens,
        file_count=len(file_contents),
    )


def _count_hits(counter: Counter[str], hints: tuple[str, ...]) -> int:
    return sum(counter.get(hint.lower(), 0) for hint in hints)


def _path_hits(path_tokens: Counter[str], hints: tuple[str, ...]) -> int:
    total = 0
    for hint in hints:
        normalized = hint.lower()
        if "/" in normalized or normalized.endswith(".cs"):
            if any(normalized in token for token in path_tokens):
                total += 2
        else:
            total += path_tokens.get(normalized, 0)
    return total


def _category_hits(categories: Counter[str], pack_id: str) -> int:
    hits = 0
    for category, count in categories.items():
        if not category:
            continue
        for hint in _CATEGORY_HINTS.get(pack_id, ()):
            if hint in category:
                hits += count * 2
    return hits


def _score_pack(pack_id: str, signals: PackSignals) -> int:
    score = 0
    score += _path_hits(signals.path_tokens, _PATH_HINTS.get(pack_id, ()))
    score += _count_hits(signals.content_tokens, _TOKEN_HINTS.get(pack_id, ()))
    score += _count_hits(signals.call_tokens, _CALL_HINTS.get(pack_id, ())) * 2
    score += _category_hits(signals.categories, pack_id)

    # Source-aware nudges are cheaper and more reliable than full-content substring scans.
    if pack_id == "binary-parsing" and score > 0 and signals.source_tokens.get("devskim", 0):
        score += 1
    if pack_id == "thread-safety-and-shared-state" and score > 0 and signals.source_tokens.get("roslyn", 0):
        score += 1
    if pack_id == "unity-lifecycle" and score > 0 and signals.file_count and "assets" in signals.path_tokens:
        score += 1

    return score


def select_prompt_pack_ids(
    file_contents: list[tuple[str, str]],
    issue_seeds: list[IssueSeed | ReviewComment] | None = None,
    stage: str = "scan",
    max_extra_packs: int = 3,
) -> list[str]:
    """Return a small ordered set of prompt pack ids for the request."""
    selected = ["core-csharp"]
    if stage == "fix":
        selected.append("autofix-safety")

    signals = _collect_signals(file_contents, issue_seeds)
    scores: dict[str, int] = defaultdict(int)

    for pack_id in (
        "unity-lifecycle",
        "binary-parsing",
        "stream-io",
        "collections-nullability",
        "save-data-integrity",
        "copy-aliasing-and-mutability",
        "thread-safety-and-shared-state",
        "unsafe-buffer-and-rle-decode",
    ):
        score = _score_pack(pack_id, signals)
        if score > 0:
            scores[pack_id] = score

    ordered_extras = [
        pack_id
        for pack_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        if score > 0
    ]
    for pack_id in ordered_extras[:max_extra_packs]:
        if pack_id not in selected:
            selected.append(pack_id)
    return selected
