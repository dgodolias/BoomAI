from __future__ import annotations

from pathlib import Path

from boomai.app.services import settings_service


def test_save_and_unset_setting_roundtrip(tmp_path: Path, monkeypatch) -> None:
    env_dir = tmp_path / ".boomai"
    env_file = env_dir / ".env"
    monkeypatch.setattr(settings_service, "GLOBAL_ENV_DIR", env_dir)
    monkeypatch.setattr(settings_service, "GLOBAL_ENV_FILE", env_file)

    settings_service.save_setting("BOOMAI_FOO", "bar")
    assert "BOOMAI_FOO=bar" in env_file.read_text(encoding="utf-8")

    settings_service.unset_setting("BOOMAI_FOO")
    assert env_file.read_text(encoding="utf-8") == ""
