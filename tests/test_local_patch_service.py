from __future__ import annotations

from pathlib import Path

from boomai.app.services.local_patch_service import find_and_replace, line_match
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
