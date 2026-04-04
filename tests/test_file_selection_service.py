from __future__ import annotations

from pathlib import Path

from boomai.app.services.file_selection_service import normalize_repo_target, select_target_files


def test_normalize_repo_target_handles_relative_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("print('x')", encoding="utf-8")
    normalized = normalize_repo_target("src/app.py", str(repo))
    assert normalized == "src/app.py"


def test_select_target_files_matches_file_and_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    all_files = ["src/a.py", "src/b.py", "docs/readme.md"]
    selected, unmatched = select_target_files(all_files, str(repo), ["src", "docs/readme.md"])
    assert selected == ["src/a.py", "src/b.py", "docs/readme.md"]
    assert unmatched == []
