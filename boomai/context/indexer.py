"""Lightweight code indexer for cross-file retrieval.

This first pass is intentionally simple and fast:
- extracts top-level C# and Python symbols with regex
- indexes namespace/import data per file
- captures identifiers as candidate references
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([A-Za-z_][\w\.]*)", re.MULTILINE)
_USING_RE = re.compile(r"^\s*using\s+([A-Za-z_][\w\.]*)\s*;", re.MULTILINE)
_TYPE_RE = re.compile(
    r"^\s*(?:public|private|internal|protected|static|sealed|abstract|partial|\s)*"
    r"\b(class|interface|struct|enum)\s+([A-Za-z_]\w*)"
    r"(?:\s*:\s*([^{]+))?",
    re.MULTILINE,
)
_METHOD_RE = re.compile(
    r"^\s*(?:public|private|internal|protected|static|virtual|override|abstract|async|sealed|partial|\s)+"
    r"(?:[A-Za-z_][\w<>\[\]\.,\? ]+\s+)+([A-Za-z_]\w*)\s*\(",
    re.MULTILINE,
)
_IDENTIFIER_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")
_PY_CLASS_RE = re.compile(r"^class\s+([A-Za-z_]\w*)(?:\(([^)]*)\))?\s*:", re.MULTILINE)
_PY_DEF_RE = re.compile(r"^(async\s+def|def)\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)
_PY_IMPORT_RE = re.compile(r"^\s*import\s+(.+)$", re.MULTILINE)
_PY_FROM_RE = re.compile(r"^\s*from\s+([A-Za-z_][\w\.]*)\s+import\s+(.+)$", re.MULTILINE)
_PY_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_CONTROL_WORDS = {"if", "for", "foreach", "while", "switch", "catch", "using", "return"}


@dataclass(frozen=True)
class SymbolDefinition:
    name: str
    kind: str
    file: str
    line: int
    namespace: str | None = None
    container: str | None = None
    bases: tuple[str, ...] = ()


@dataclass
class CodeIndex:
    symbols_by_name: dict[str, list[SymbolDefinition]] = field(default_factory=dict)
    file_symbols: dict[str, list[str]] = field(default_factory=dict)
    file_identifiers: dict[str, set[str]] = field(default_factory=dict)
    file_usings: dict[str, list[str]] = field(default_factory=dict)
    file_namespaces: dict[str, str | None] = field(default_factory=dict)


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _normalize_base_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    result: list[str] = []
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        token = token.split("<", 1)[0].strip()
        token = token.rsplit(".", 1)[-1]
        if token:
            result.append(token)
    return tuple(result)


def _index_csharp_file(path: str, content: str, index: CodeIndex) -> None:
    namespace_match = _NAMESPACE_RE.search(content)
    namespace = namespace_match.group(1) if namespace_match else None
    index.file_namespaces[path] = namespace
    index.file_usings[path] = [m.group(1) for m in _USING_RE.finditer(content)]

    file_symbols: list[str] = []

    for match in _TYPE_RE.finditer(content):
        kind, name, bases_raw = match.groups()
        definition = SymbolDefinition(
            name=name,
            kind=kind,
            file=path,
            line=_line_number(content, match.start()),
            namespace=namespace,
            bases=_normalize_base_list(bases_raw),
        )
        index.symbols_by_name.setdefault(name, []).append(definition)
        file_symbols.append(name)

    for match in _METHOD_RE.finditer(content):
        name = match.group(1)
        if name in _CONTROL_WORDS:
            continue
        definition = SymbolDefinition(
            name=name,
            kind="method",
            file=path,
            line=_line_number(content, match.start()),
            namespace=namespace,
        )
        index.symbols_by_name.setdefault(name, []).append(definition)
        file_symbols.append(name)

    index.file_symbols[path] = file_symbols
    index.file_identifiers[path] = set(_IDENTIFIER_RE.findall(content))


def _index_python_file(path: str, content: str, index: CodeIndex) -> None:
    module_name = path.replace("\\", "/").rsplit(".", 1)[0].replace("/", ".")
    index.file_namespaces[path] = module_name

    imports: list[str] = []
    for match in _PY_IMPORT_RE.finditer(content):
        for chunk in match.group(1).split(","):
            token = chunk.strip().split(" as ", 1)[0].strip()
            if token:
                imports.append(token.rsplit(".", 1)[-1])
    for match in _PY_FROM_RE.finditer(content):
        module = match.group(1)
        imports.append(module.rsplit(".", 1)[-1])
        for chunk in match.group(2).split(","):
            token = chunk.strip().split(" as ", 1)[0].strip()
            if token and token != "*":
                imports.append(token)
    index.file_usings[path] = imports

    file_symbols: list[str] = []

    for match in _PY_CLASS_RE.finditer(content):
        name = match.group(1)
        bases = _normalize_base_list(match.group(2))
        definition = SymbolDefinition(
            name=name,
            kind="class",
            file=path,
            line=_line_number(content, match.start()),
            namespace=module_name,
            bases=bases,
        )
        index.symbols_by_name.setdefault(name, []).append(definition)
        file_symbols.append(name)

    for match in _PY_DEF_RE.finditer(content):
        name = match.group(2)
        definition = SymbolDefinition(
            name=name,
            kind="function",
            file=path,
            line=_line_number(content, match.start()),
            namespace=module_name,
        )
        index.symbols_by_name.setdefault(name, []).append(definition)
        file_symbols.append(name)

    index.file_symbols[path] = file_symbols
    index.file_identifiers[path] = set(_PY_IDENTIFIER_RE.findall(content))


def build_code_index(
    file_contents: list[tuple[str, str]],
    detected_languages: list[str] | None = None,
) -> CodeIndex:
    """Build a lightweight symbol/index map for retrieval."""
    detected_languages = detected_languages or []
    index = CodeIndex()

    for path, content in file_contents:
        ext = os.path.splitext(path)[1].lower()
        if "csharp" in detected_languages or ext == ".cs":
            _index_csharp_file(path, content, index)
        elif ext == ".py":
            _index_python_file(path, content, index)

    return index
