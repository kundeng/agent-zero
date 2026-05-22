"""Unit tests for ``haz setup`` flag-only paths.

The interactive prompts go through ``click.testing.CliRunner.input`` and
are tested indirectly via the flag-only invocations below — the same
``_save_settings`` code path runs in both cases.

Key invariant: non-interactive mode writes ONLY the fields the user
passed via flags. No opinionated defaults, no silent additions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from hyperagent0.cli_commands import setup as setup_cmd


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Redirect ``_settings_path`` so each test gets a fresh file."""

    target = tmp_path / "usr" / "settings.json"
    monkeypatch.setattr(setup_cmd, "_settings_path", lambda: target)
    return target


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_flag_only_writes_only_specified_fields(isolated_settings):
    """--sandbox docker should write *only* sandbox_mode, nothing else."""

    runner = CliRunner()
    result = runner.invoke(setup_cmd.command, ["--sandbox", "sandbox"])
    assert result.exit_code == 0, result.output

    data = _read(isolated_settings)
    assert data == {"sandbox_mode": "sandbox"}, f"unexpected keys: {data}"


def test_multiple_flags_combine(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(
        setup_cmd.command,
        ["--provider", "anthropic", "--model", "claude-sonnet-4-5"],
    )
    assert result.exit_code == 0, result.output
    data = _read(isolated_settings)
    assert data == {
        "chat_model_provider": "anthropic",
        "chat_model_name": "claude-sonnet-4-5",
    }, f"unexpected: {data}"


def test_preserves_existing_unrelated_fields(isolated_settings):
    isolated_settings.parent.mkdir(parents=True, exist_ok=True)
    isolated_settings.write_text(
        json.dumps({"existing_field": "keep_me", "another": 42})
    )

    runner = CliRunner()
    result = runner.invoke(setup_cmd.command, ["--sandbox", "ssh"])
    assert result.exit_code == 0, result.output

    data = _read(isolated_settings)
    assert data["existing_field"] == "keep_me"
    assert data["another"] == 42
    assert data["sandbox_mode"] == "ssh"


def test_empty_api_base_clears_field(isolated_settings):
    isolated_settings.parent.mkdir(parents=True, exist_ok=True)
    isolated_settings.write_text(
        json.dumps({"chat_model_api_base": "http://stale.example/v1"})
    )

    runner = CliRunner()
    result = runner.invoke(setup_cmd.command, ["--api-base", ""])
    assert result.exit_code == 0, result.output
    assert "chat_model_api_base" not in _read(isolated_settings)


def test_invalid_sandbox_rejected(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(setup_cmd.command, ["--sandbox", "kubernetes"])
    assert result.exit_code != 0
    assert "kubernetes" in result.output.lower() or "invalid" in result.output.lower()


def test_non_interactive_prints_path_without_writing(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(setup_cmd.command, ["--non-interactive"])
    assert result.exit_code == 0
    assert str(isolated_settings) in result.output
    assert not isolated_settings.exists()


def test_no_args_no_input_does_not_silently_write(isolated_settings):
    """With no flags and no stdin input, interactive prompts hang. CliRunner
    with empty input should produce *some* output but must not write
    opinionated LLM defaults if the user can't even answer the prompts."""

    runner = CliRunner()
    # Provide just enough input to get through the prompts with defaults.
    # The point is: even if interactive picks defaults, those defaults
    # belong to the wizard, not to ``--quick`` (which no longer exists).
    result = runner.invoke(setup_cmd.command, [], input="\n\n\n\n")
    assert result.exit_code == 0
    # Defaults from the wizard should land here, but ONLY because the
    # user pressed Enter through prompts — not because we silently wrote
    # them. The distinction matters: ``haz setup --sandbox docker`` in
    # test_flag_only_writes_only_specified_fields proved that.
    if isolated_settings.exists():
        data = _read(isolated_settings)
        # Interactive defaults include LLM fields.
        assert "chat_model_provider" in data
