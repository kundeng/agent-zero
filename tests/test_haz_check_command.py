"""Unit tests for ``haz check`` that exercise the no-network code paths.

The real network call (litellm.completion) is deliberately not mocked
here — that would add a runtime dep on the ``unittest.mock`` style
specifics of LiteLLM, which we don't control. Instead we verify the
guards that fire BEFORE the network call: missing settings file,
missing fields, unsupported provider, flag overrides reaching the
call path.

Exit codes the tests pin down:

  1  no settings file
  2  required field missing
  4  claude-sdk provider (not yet implemented)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from hyperagent0.cli_commands import check as check_cmd


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Redirect the settings path so each test gets a fresh file."""

    target = tmp_path / "usr" / "settings.json"
    monkeypatch.setattr(
        check_cmd._paths, "settings_path", lambda: target
    )
    return target


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_no_settings_file_exits_1(isolated_settings):
    runner = CliRunner()
    result = runner.invoke(check_cmd.command, [])
    assert result.exit_code == 1
    assert "no settings file" in (result.output + (result.stderr if hasattr(result, "stderr") else ""))


def test_missing_provider_exits_2(isolated_settings):
    _write_settings(isolated_settings, {"chat_model_name": "x"})
    runner = CliRunner()
    result = runner.invoke(check_cmd.command, [])
    assert result.exit_code == 2


def test_missing_model_exits_2(isolated_settings):
    _write_settings(isolated_settings, {"chat_model_provider": "openai"})
    runner = CliRunner()
    result = runner.invoke(check_cmd.command, [])
    assert result.exit_code == 2


def test_claude_sdk_provider_exits_4(isolated_settings):
    _write_settings(
        isolated_settings,
        {"chat_model_provider": "claude-sdk", "chat_model_name": "anything"},
    )
    runner = CliRunner()
    result = runner.invoke(check_cmd.command, [])
    assert result.exit_code == 4


def test_flag_override_takes_precedence(isolated_settings, monkeypatch):
    """The --model and --api-base flags should override settings values
    BEFORE the LLM call happens. We capture the call to verify."""

    _write_settings(
        isolated_settings,
        {
            "chat_model_provider": "openai",
            "chat_model_name": "stale-from-settings",
            "chat_model_api_base": "http://stale.example",
        },
    )

    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return {
            "choices": [{"message": {"content": "ok"}}],
        }

    # Patch the import that happens inside command(). We import the
    # litellm module attribute path the command uses.
    import litellm  # noqa: F401  (force-load before monkeypatch so import inside command succeeds)

    monkeypatch.setattr("litellm.completion", fake_completion)

    runner = CliRunner()
    result = runner.invoke(
        check_cmd.command,
        ["--model", "override-model", "--api-base", "http://override.example"],
    )
    assert result.exit_code == 0, result.output
    assert captured["model"] == "override-model"
    assert captured["api_base"] == "http://override.example"


def test_anthropic_provider_gets_prefixed(isolated_settings, monkeypatch):
    """For non-openai providers without a prefix in the model name, the
    LiteLLM model arg should be ``provider/model``."""

    _write_settings(
        isolated_settings,
        {
            "chat_model_provider": "anthropic",
            "chat_model_name": "claude-sonnet-4-5",
        },
    )

    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    import litellm  # noqa: F401
    monkeypatch.setattr("litellm.completion", fake_completion)

    runner = CliRunner()
    result = runner.invoke(check_cmd.command, [])
    assert result.exit_code == 0, result.output
    assert captured["model"] == "anthropic/claude-sonnet-4-5"


def test_openai_provider_not_prefixed(isolated_settings, monkeypatch):
    """openai-compatible setups (incl. the local proxy) leave model
    name verbatim; LiteLLM uses api_base to route."""

    _write_settings(
        isolated_settings,
        {
            "chat_model_provider": "openai",
            "chat_model_name": "cc/claude-sonnet-4-6",
            "chat_model_api_base": "http://localhost:20128",
        },
    )

    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    import litellm  # noqa: F401
    monkeypatch.setattr("litellm.completion", fake_completion)

    runner = CliRunner()
    result = runner.invoke(check_cmd.command, [])
    assert result.exit_code == 0, result.output
    assert captured["model"] == "cc/claude-sonnet-4-6"
    assert captured["api_base"] == "http://localhost:20128"


def test_completion_exception_exits_3(isolated_settings, monkeypatch):
    _write_settings(
        isolated_settings,
        {"chat_model_provider": "openai", "chat_model_name": "x"},
    )

    def boom(**_):
        raise RuntimeError("simulated network failure")

    import litellm  # noqa: F401
    monkeypatch.setattr("litellm.completion", boom)

    runner = CliRunner()
    result = runner.invoke(check_cmd.command, [])
    assert result.exit_code == 3
    # Click's mix_stderr=False default makes stderr separate; check both.
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "simulated network failure" in combined or "FAIL" in combined
