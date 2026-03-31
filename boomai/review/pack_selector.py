"""Heuristic selection of prompt packs for BoomAI."""

from __future__ import annotations

from collections import defaultdict

from ..core.models import IssueSeed, ReviewComment


def _normalize_text(value: str) -> str:
    return value.lower().replace("\\", "/")


def _collect_haystack(
    file_contents: list[tuple[str, str]],
    issue_seeds: list[IssueSeed | ReviewComment] | None = None,
) -> tuple[str, str]:
    path_text = "\n".join(_normalize_text(path) for path, _ in file_contents)
    content_text = "\n".join(content.lower() for _, content in file_contents)
    if issue_seeds:
        seed_text = "\n".join(
            f"{getattr(seed, 'file', '')} {getattr(seed, 'body', getattr(seed, 'message', ''))}".lower()
            for seed in issue_seeds
        )
        content_text = f"{content_text}\n{seed_text}"
    return path_text, content_text


def _signal(score: bool) -> int:
    return 1 if score else 0


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

    path_text, content_text = _collect_haystack(file_contents, issue_seeds)
    scores: dict[str, int] = defaultdict(int)

    unity_score = (
        _signal("assets/scripts" in path_text)
        + _signal(any(token in content_text for token in (
            "monobehaviour", "scriptableobject", "fixedupdate(", "update(",
            "ondestroy(", "onenable(", "ondisable(", "awake(", "start(",
            "getcomponent(", "findobjectoftype(", "gameobject.find(",
        )))
    )
    if unity_score:
        scores["unity-lifecycle"] += unity_score

    binary_score = (
        _signal(any(token in path_text for token in ("/api/", "file.cs", "/formats/", "/reader/")))
        + _signal(any(token in content_text for token in (
            "binaryreader", "readbytes(", "seek(", "offset", "recordcount",
            "position", "byte[]", "byte [", "record length", "basestream.position",
            "overflowexception", "framecount", "width * height", "height * width",
        )))
        + _signal(any(token in content_text for token in (
            "indexoutofrange", "outofmemory", "eof", "malformed file", "array allocation",
            "partial read", "truncated", "unterminated string", "invalid data",
        )))
    )
    if binary_score:
        scores["binary-parsing"] += binary_score

    stream_score = (
        _signal(any(token in content_text for token in (
            "stream", "filestream", "streamreader", "streamwriter", "binarywriter",
            "dispose(", "close(", "flush(", "read(", "write(",
        )))
        + _signal(any(token in content_text for token in (
            "partial read", "file handle", "resource leak", "using statement",
            "leaveopen", "leave open", "underlying stream", "readbytes return value ignored",
        )))
    )
    if stream_score:
        scores["stream-io"] += stream_score

    collections_score = (
        _signal(any(token in content_text for token in (
            "dictionary<", "trygetvalue", "containskey", "list<", "hashset<",
            "nullreference", "null check", "[0]", "collection was modified",
        )))
        + _signal(any(token in content_text for token in (
            "keynotfound", "missing key", "null/empty", "tokens array", "effectsplit array",
            "invalidoperationexception", "enumeration operation may not execute",
        )))
    )
    if collections_score:
        scores["collections-nullability"] += collections_score

    save_score = (
        _signal(any(token in path_text for token in ("/save/", "savegames", "savetree", "characterrecord")))
        + _signal(any(token in content_text for token in (
            "serialize", "deserialize", "savegame", "recorddata", "streamlength",
            "truncate", "corrupt", "save file", "clone(", "copyto(", "memberwiseclone",
            "recordid", "parseddata", "shallow copy",
        )))
    )
    if save_score:
        scores["save-data-integrity"] += save_score

    aliasing_score = (
        _signal(any(token in path_text for token in ("/save/", "/record", "/cache", "/model", "/data/")))
        + _signal(any(token in content_text for token in (
            "clone(", "copyto(", "memberwiseclone", "shallow copy", "parseddata",
            "magic = ", "children = ", "deep copy", "defensive copy",
        )))
        + _signal(any(token in content_text for token in (
            "cached", "shared state", "mutable state", "alias", "reference field", "copied by value",
        )))
    )
    if aliasing_score:
        scores["copy-aliasing-and-mutability"] += aliasing_score

    thread_safety_score = (
        _signal(any(token in content_text for token in (
            "static ", "lock (", "concurrentdictionary", "interlocked", "volatile ",
            "thread", "task.run", "random.", "seed", "shared", "cache",
        )))
        + _signal(any(token in content_text for token in (
            "not thread-safe", "race condition", "concurrent access", "shared dictionary",
            "global state", "shared state corruption",
        )))
    )
    if thread_safety_score:
        scores["thread-safety-and-shared-state"] += thread_safety_score

    decode_score = (
        _signal(any(token in path_text for token in (
            "gfxfile.cs", "flcfile.cs", "cfafile.cs", "imgfile.cs", "pakfile.cs",
            "vidfile.cs", "skyfile.cs", "baseimagefile.cs", "texturefile.cs",
        )))
        + _signal(any(token in content_text for token in (
            "rle", "framebuffer", "palette", "dstpos", "srcpos", "rowpos",
            "array.copy", "buffer.blockcopy", "decode", "probe", "framecount",
        )))
        + _signal(any(token in content_text for token in (
            "screen_copy", "screen_repeat", "pakextractedbuffer", "frame buffer",
            "colorind", "partial frame", "run-length",
        )))
    )
    if decode_score:
        scores["unsafe-buffer-and-rle-decode"] += decode_score

    ordered_extras = [
        pack_id
        for pack_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        if score > 0
    ]
    for pack_id in ordered_extras[:max_extra_packs]:
        if pack_id not in selected:
            selected.append(pack_id)
    return selected
