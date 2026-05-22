"""``haz start`` — launch the hyperagent0 daemon (foreground by default).

Behavior summary (spec 03 D6, task 1.4):

* Default: foreground. Logs stream to stdout, ``Ctrl-C`` triggers a
  graceful shutdown via the signal handler installed below.
* ``-d`` / ``--daemon``: classic Unix double-fork, redirect stdout and
  stderr into ``~/.hyperagent0/logs/daemon.log``, return to the shell
  once the child has acquired the singleton lock.
* ``--systemd``: foreground (systemd manages the lifecycle), no
  daemonization, no PID file write — systemd owns the PID.

All heavy imports (``run_ui.start_server``, Flask, LiteLLM) happen
inside :func:`_run_server`. Keeping them out of the module body
preserves the cold-start budget for everyone who imports this file
just to register the Click command via :mod:`hyperagent0.cli`.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import click

from .. import daemon as _daemon


def _install_signal_handlers(lock_handle) -> None:
    """Install SIGTERM/SIGINT handlers that drive a graceful shutdown.

    Spec 03 task 1.6: pause all AgentContexts, wait for in-flight tools,
    persist state, close connections. We delegate to
    :func:`hyperagent0.shutdown.graceful_shutdown` so the hook stays out
    of the agent core (no agent.py patch needed).
    """

    _shutting_down = {"flag": False}

    def _handler(signum, _frame):  # noqa: ANN001 - signal handler signature
        if _shutting_down["flag"]:
            # Second signal — escalate.
            click.echo("[hyperagent0] forced exit", err=True)
            os._exit(1)
        _shutting_down["flag"] = True
        signame = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        click.echo(f"[hyperagent0] received {signame}, shutting down...", err=True)
        # Import here so even SIGTERM handling stays out of the cold
        # path until the daemon is actually live.
        try:
            from ..shutdown import graceful_shutdown

            graceful_shutdown(timeout=25.0)
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            click.echo(f"[hyperagent0] shutdown hook error: {exc}", err=True)
        finally:
            _daemon.release_lock(lock_handle)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


_SETTINGS_ENV_VARS: dict[str, str] = {
    # Settings.json key  <-  Environment variable name
    "chat_model_provider": "CHAT_MODEL_PROVIDER",
    "chat_model_name": "CHAT_MODEL_NAME",
    "chat_model_api_base": "CHAT_MODEL_API_BASE",
    "sandbox_mode": "SANDBOX_MODE",
}


def _apply_env_defaults_to_settings() -> None:
    """Write env-var defaults into ``usr/settings.json`` on first start.

    Compose users (Journey D) set ``SANDBOX_MODE`` / ``CHAT_MODEL_*`` in
    their ``.env``. The container reads them here and persists them as
    defaults — but ONLY for fields not already set. That way settings
    configured later via the web UI win on subsequent restarts, and a
    docker-compose restart doesn't clobber what the user picked.

    Stdlib-only so the cold-start budget for ``haz start`` (which goes
    on to launch the whole agent stack anyway) is unaffected.
    """

    import json
    import os
    from .. import paths as _paths

    pending: dict[str, str] = {}
    for setting_key, env_name in _SETTINGS_ENV_VARS.items():
        value = os.environ.get(env_name)
        if value:
            pending[setting_key] = value

    if not pending:
        return

    path = _paths.settings_path()
    current: dict = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}

    # Only apply env vars for keys NOT already in settings.json.
    # This is "default on first boot," not "force on every boot."
    changed = False
    for key, value in pending.items():
        if key not in current or not current[key]:
            current[key] = value
            changed = True

    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")


def _run_server(host: str | None, port: int | None) -> None:
    """Call into ``run_ui.start_server`` with the given overrides.

    Imports inside the function body — see module docstring.
    """

    # The CLI lives inside the hyperagent0 wrapper package; ``run_ui`` is
    # the upstream Agent Zero entry point at the repo root. Make sure
    # the repo root is importable when the CLI is invoked from an
    # arbitrary cwd via a console_script.
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import run_ui  # type: ignore[import-not-found]

    # Boot channel adapters (spec 04 task 1.6) on a background thread
    # *before* the (blocking) HTTP server call so they're live by the
    # time the first request arrives. Import is deferred — direct
    # ``python run_ui.py`` invocations never load this module, so they
    # stay web-UI-only as the spec requires.
    try:
        from ..channels.lifecycle import start_enabled_channels

        start_enabled_channels()
    except Exception as exc:
        click.echo(f"[hyperagent0] channel startup error: {exc}", err=True)

    run_ui.start_server(host=host, port=port)


def _daemonize(log_path: Path) -> None:
    """Classic Unix double-fork. The grandchild returns; everything
    else exits. Stdout/stderr are redirected to ``log_path``."""

    # First fork.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    # Second fork — orphan from the controlling terminal.
    if os.fork() > 0:
        os._exit(0)

    os.chdir("/")
    os.umask(0o022)

    # Reopen stdio. /dev/null for stdin, log file for stdout/stderr.
    sys.stdout.flush()
    sys.stderr.flush()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(devnull_fd, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(devnull_fd)
    os.close(log_fd)


@click.command("start")
@click.option(
    "-d",
    "--daemon",
    "daemonize",
    is_flag=True,
    default=False,
    help="Double-fork and run as a background daemon (logs redirected).",
)
@click.option(
    "--systemd",
    is_flag=True,
    default=False,
    help="Foreground systemd-friendly mode: no daemonization, no PID file write.",
)
@click.option(
    "--host",
    default=None,
    metavar="HOST",
    help="Bind host (default: localhost or WEB_UI_HOST env).",
)
@click.option(
    "--lan",
    is_flag=True,
    default=False,
    help="Shortcut for --host 0.0.0.0 — bind on every interface so the UI is reachable from the LAN.",
)
@click.option(
    "--port",
    type=int,
    default=None,
    metavar="PORT",
    help="Bind port (default: WEB_UI_PORT env var, else 50080).",
)
def command(daemonize: bool, systemd: bool, host: str | None, lan: bool, port: int | None) -> None:
    """Launch the hyperagent0 daemon (web UI + agent runtime).

    Default is foreground — ``Ctrl-C`` stops cleanly. Pass ``-d`` to
    detach into a background daemon, or ``--systemd`` when running
    under a systemd unit.
    """

    # --lan resolves before --host so an explicit --host still wins.
    if lan and host is None:
        host = "0.0.0.0"
    if lan and host not in (None, "0.0.0.0"):
        raise click.UsageError("--lan and --host are mutually exclusive.")

    # Resolve host: explicit flag > WEB_UI_HOST env var > upstream default
    # (None means run_ui.start_server picks localhost). Docker / compose
    # deployments set WEB_UI_HOST=0.0.0.0 in the image env so the UI is
    # reachable from outside the container without needing --host on CMD.
    if host is None:
        host = os.environ.get("WEB_UI_HOST") or None

    # Standardize on port 50080 across host and container installs.
    # Upstream's run_ui defaults to 5000; overriding here keeps the
    # README's "open http://localhost:50080" advice consistent
    # regardless of how the user installed.
    if port is None:
        port = int(os.environ.get("WEB_UI_PORT", "50080"))

    # First-boot defaults from env: SANDBOX_MODE, CHAT_MODEL_*. Idempotent
    # — only writes fields not already present in usr/settings.json. See
    # spec 07 P2.1 for the compose-deployment motivation.
    _apply_env_defaults_to_settings()

    if daemonize and systemd:
        raise click.UsageError("--daemon and --systemd are mutually exclusive.")

    if systemd:
        # Systemd owns the PID and lifecycle; no lock/pidfile here.
        _install_signal_handlers(None)
        _run_server(host, port)
        return

    # Idempotency: if a daemon is already running, surface its status
    # and exit 0 rather than racing on the lock.
    if _daemon.is_running():
        pid = _daemon.get_pid()
        click.echo(
            f"hyperagent0 is already running (PID {pid}). Run 'haz status' for details."
        )
        return

    if daemonize:
        log_path = _daemon.logs_dir() / "daemon.log"
        click.echo(f"hyperagent0: detaching; logs -> {log_path}")
        _daemonize(log_path)
        # In the grandchild now. Acquire the lock and run the server.
        handle = _daemon.acquire_lock()
        if handle is None:
            # Lost the race with a parallel start.
            sys.exit(0)
        _install_signal_handlers(handle)
        try:
            _run_server(host, port)
        finally:
            _daemon.release_lock(handle)
        return

    # Foreground.
    handle = _daemon.acquire_lock()
    if handle is None:
        pid = _daemon.get_pid()
        click.echo(
            f"hyperagent0 is already running (PID {pid}). Run 'haz status' for details."
        )
        return
    _install_signal_handlers(handle)
    click.echo(f"hyperagent0: starting (PID {os.getpid()})")
    try:
        _run_server(host, port)
    finally:
        _daemon.release_lock(handle)
