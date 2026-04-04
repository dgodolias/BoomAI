from __future__ import annotations

import pytest

from boomai.cli import build_parser


def test_cli_help_lists_fix_and_settings(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    output = capsys.readouterr().out
    assert "boomai fix" in output
    assert "boomai settings" in output


def test_fix_subcommand_keeps_expected_flags(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fix", "--help"])
    output = capsys.readouterr().out
    assert "--deep" in output
    assert "--cost-report" in output
    assert "--clean-run" in output
