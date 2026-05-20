"""Sandbox registry smoke tests (spec 01-host-first task 1.4)."""

import shutil

import pytest


def test_registry_lists_builtin_modes():
    from hyperagent0.sandbox import registered_modes

    modes = registered_modes()
    for required in ("none", "sandbox", "ssh"):
        assert required in modes


def test_none_backend_is_always_available():
    from hyperagent0.sandbox import get_backend
    from hyperagent0.sandbox.none import NoneBackend

    backend = get_backend("none")
    assert isinstance(backend, NoneBackend)
    assert NoneBackend.is_available() is True


def test_unknown_mode_raises():
    from hyperagent0.sandbox import get_backend

    with pytest.raises(ValueError, match="unknown sandbox_mode"):
        get_backend("totally-not-a-mode")


def test_srt_backend_skipped_if_unavailable():
    from hyperagent0.sandbox import SandboxUnavailableError, get_backend

    if shutil.which("srt") is None:
        with pytest.raises(SandboxUnavailableError):
            get_backend("sandbox")
    else:
        backend = get_backend("sandbox")
        assert backend.mode == "sandbox"


def test_ssh_backend_available_via_paramiko():
    pytest.importorskip("paramiko")
    from hyperagent0.sandbox import get_backend

    backend = get_backend("ssh")
    assert backend.mode == "ssh"


def test_register_backend_allows_external_modes():
    from hyperagent0.sandbox import (
        SandboxBackend,
        get_backend,
        register_backend,
        registered_modes,
    )

    class _Fake(SandboxBackend):
        mode = "fake"

        @classmethod
        def is_available(cls) -> bool:
            return True

        async def open_shell(self, cwd=None):  # pragma: no cover - not exercised
            return object()

    register_backend("fake", _Fake)
    assert "fake" in registered_modes()
    inst = get_backend("fake")
    assert isinstance(inst, _Fake)


@pytest.mark.asyncio
async def test_none_backend_can_open_shell(tmp_path):
    """End-to-end smoke: NoneBackend.open_shell yields a connected session."""
    from hyperagent0.sandbox import get_backend

    backend = get_backend("none")
    try:
        session = await backend.open_shell(cwd=str(tmp_path))
    except Exception as e:
        # On constrained CI runners, /bin/bash or PTY may not be reachable.
        pytest.skip(f"local PTY unavailable in this environment: {e}")
    try:
        assert session is not None
        # Duck-typed interface check.
        assert hasattr(session, "send_command")
        assert hasattr(session, "read_output")
    finally:
        await session.close()
