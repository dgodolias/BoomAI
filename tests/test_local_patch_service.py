from __future__ import annotations

from pathlib import Path

from boomai.app.services.local_patch_service import (
    detect_newline_style,
    find_and_replace,
    line_match,
)
from boomai.core.models import ReviewComment, Severity


def test_line_match_finds_indentation_tolerant_match() -> None:
    content = "if test:\n    print('a')\n    print('b')\n"
    old_code = "print('a')\nprint('b')"
    match = line_match(content, old_code, 2)
    assert match == (1, 2)


def test_find_and_replace_replaces_code_block() -> None:
    content = "x = 1\ny = 2\n"
    new_content, ok = find_and_replace(content, "y = 2", "y = 3", 2)
    assert ok is True
    assert "y = 3" in new_content


def test_apply_like_contract_uses_same_shape(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.py"
    file_path.write_text("x = 1\ny = 2\n", encoding="utf-8")
    finding = ReviewComment(
        file="sample.py",
        line=2,
        severity=Severity.MEDIUM,
        body="update value",
        old_code="y = 2",
        suggestion="y = 3",
    )
    from boomai.app.services.local_patch_service import apply_local

    applied = apply_local([finding], str(tmp_path))
    assert applied == 1
    assert file_path.read_text(encoding="utf-8") == "x = 1\ny = 3\n"


def test_detect_newline_style_prefers_crlf() -> None:
    assert detect_newline_style("a\r\nb\r\n") == "\r\n"
    assert detect_newline_style("a\nb\n") == "\n"


def test_apply_local_preserves_original_crlf_line_endings(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.cs"
    with open(file_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("x = 1;\r\ny = 2;\r\n")
    finding = ReviewComment(
        file="sample.cs",
        line=2,
        severity=Severity.MEDIUM,
        body="update value",
        old_code="y = 2;",
        suggestion="y = 3;",
    )
    from boomai.app.services.local_patch_service import apply_local

    applied = apply_local([finding], str(tmp_path))
    assert applied == 1
    with open(file_path, "r", encoding="utf-8", newline="") as handle:
        assert handle.read() == "x = 1;\r\ny = 3;\r\n"
