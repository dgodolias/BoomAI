from __future__ import annotations

import difflib
import os


def detect_newline_style(content: str) -> str:
    if "\r\n" in content:
        return "\r\n"
    if "\r" in content:
        return "\r"
    return "\n"


def normalize_newlines(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def restore_newlines(content: str, newline_style: str) -> str:
    if newline_style == "\n":
        return content
    return content.replace("\n", newline_style)


def is_natural_language(suggestion: str) -> bool:
    s = suggestion.strip()
    instruction_starters = (
        "Remove ", "Delete ", "Replace ", "Update ", "Change ",
        "Rename ", "Move ", "Add ", "Ensure ", "Consider ",
        "Refactor ", "Fix ", "Implement ", "Use ", "Convert ",
    )
    if s.startswith(instruction_starters):
        return True
    code_chars = {"{", "}", "(", ")", ";", "=", "<", ">", "[", "]"}
    return not any(char in s for char in code_chars)


def line_match(content: str, old_code: str, hint_line: int) -> tuple[int, int] | None:
    content_lines = content.split("\n")
    old_lines = old_code.strip().split("\n")
    old_stripped = [line.rstrip() for line in old_lines]

    while old_stripped and not old_stripped[0].strip():
        old_stripped.pop(0)
    while old_stripped and not old_stripped[-1].strip():
        old_stripped.pop()

    if not old_stripped:
        return None

    line_count = len(old_stripped)
    best: tuple[int, int] | None = None
    best_distance = float("inf")
    for index in range(len(content_lines) - line_count + 1):
        window = [content_lines[index + offset].rstrip() for offset in range(line_count)]
        if window == old_stripped:
            distance = abs(index - (hint_line - 1))
            if distance < best_distance:
                best_distance = distance
                best = (index, line_count)
    if best is not None:
        return best

    fully_stripped = [line.strip() for line in old_stripped]
    best_distance = float("inf")
    for index in range(len(content_lines) - line_count + 1):
        window = [content_lines[index + offset].strip() for offset in range(line_count)]
        if window == fully_stripped:
            distance = abs(index - (hint_line - 1))
            if distance < best_distance:
                best_distance = distance
                best = (index, line_count)
    if best is not None:
        return best

    max_start = len(content_lines) - line_count
    if max_start < 0:
        return None
    search_start = min(max_start, max(0, hint_line - 1 - 25))
    search_end = max(search_start, min(max_start, hint_line - 1 + 25))
    best_score = 0.0
    old_joined = "\n".join(fully_stripped)
    for index in range(search_start, search_end + 1):
        window = [content_lines[index + offset].strip() for offset in range(line_count)]
        score = difflib.SequenceMatcher(None, old_joined, "\n".join(window)).ratio()
        if score > best_score:
            best_score = score
            best = (index, line_count)

    if best is not None and best_score >= 0.94:
        return best
    return None


def find_and_replace(content: str, old_code: str, new_code: str, hint_line: int) -> tuple[str, bool]:
    content = normalize_newlines(content)
    old_code = normalize_newlines(old_code)
    new_code = normalize_newlines(new_code)

    if old_code in content and content.count(old_code) == 1:
        return content.replace(old_code, new_code, 1), True

    match = line_match(content, old_code, hint_line)
    if match:
        start_index, line_count = match
        lines = content.split("\n")
        replacement = new_code.split("\n") if new_code else []
        lines[start_index:start_index + line_count] = replacement
        return "\n".join(lines), True

    return content, False


def apply_local(findings: list, repo_path: str = ".", file_filter: str | None = None) -> int:
    by_file: dict[str, list] = {}
    for finding in findings:
        if finding.suggestion is None or not finding.old_code:
            continue
        if finding.suggestion and is_natural_language(finding.suggestion):
            continue
        if is_natural_language(finding.old_code):
            continue
        if not finding.suggestion and len(finding.old_code.strip().splitlines()) > 50:
            continue
        if file_filter and finding.file != file_filter:
            continue
        by_file.setdefault(finding.file, []).append(finding)

    if not by_file:
        print("  No applicable suggestions found.")
        return 0

    applied = 0
    for filepath, file_findings in by_file.items():
        full_path = os.path.join(repo_path, filepath)
        if not os.path.exists(full_path):
            print(f"  SKIP {filepath} (file not found locally)")
            continue

        with open(full_path, "r", encoding="utf-8", newline="") as handle:
            raw_content = handle.read()
        newline_style = detect_newline_style(raw_content)
        content = normalize_newlines(raw_content)

        file_applied = 0
        for finding in sorted(file_findings, key=lambda item: item.line, reverse=True):
            content, ok = find_and_replace(content, finding.old_code, finding.suggestion, finding.line)
            if ok:
                file_applied += 1
            else:
                print(f"  SKIP {filepath}:{finding.line} (code not found)")

        if file_applied:
            with open(full_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(restore_newlines(content, newline_style))
            print(f"  Applied {file_applied} fix(es) to {filepath}")
            applied += file_applied

    return applied
