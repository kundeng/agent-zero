"""``haz uninstall`` — undo what install.sh did.

Reverses the install layout described in spec 07 D3:

    ~/.hyperagent0/{repo,venv,logs}/
    ~/.local/bin/{haz,hyperagent0}

Auto-detects the install prefix from this binary's own location, so it
works whether the user installed with the default prefix or
``--prefix DIR``. Refuses to touch a directory that doesn't look like
a hyperagent0 install (missing ``venv/bin/haz``).

Stops a running daemon first. Prompts for confirmation unless ``-y``.
``--keep-state`` preserves ``repo/usr/`` (settings, projects) and
``logs/`` for post-mortem; useful when bouncing to a clean install
without losing project data.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import click

from .. import daemon as _daemon


def _detect_prefix() -> Path:
    """Walk back from the running interpreter to find the install prefix.

    ``sys.prefix`` for our venv is ``<PREFIX>/venv``. Anything else means
    we're being invoked from a system Python (unusual) — fall back to
    ``~/.hyperagent0`` and let the sanity check catch mismatches.
    """

    venv = Path(sys.prefix).resolve()
    if venv.name == "venv" and (venv / "bin" / "haz").is_file():
        return venv.parent
    # Fallback for unusual invocations.
    return Path.home() / ".hyperagent0"


def _bin_links() -> list[Path]:
    """Symlinks install.sh writes under ``~/.local/bin``."""

    bin_dir = Path.home() / ".local" / "bin"
    return [bin_dir / "haz", bin_dir / "hyperagent0"]


def _resolves_into(link: Path, prefix: Path) -> bool:
    """True if ``link`` is a symlink pointing inside ``prefix``."""

    if not link.is_symlink():
        return False
    try:
        return prefix.resolve() in link.resolve().parents
    except (OSError, RuntimeError):
        return False


def _dir_size(path: Path) -> int:
    """Best-effort recursive disk usage in bytes."""

    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


@click.command("uninstall")
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt.",
)
@click.option(
    "--keep-state",
    is_flag=True,
    default=False,
    help="Preserve repo/usr (settings, projects) and logs/ for post-mortem.",
)
@click.option(
    "--prefix",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the auto-detected install prefix.",
)
def command(yes: bool, keep_state: bool, prefix: Path | None) -> None:
    """Remove the hyperagent0 install (venv, repo clone, symlinks)."""

    target = (prefix or _detect_prefix()).resolve()

    # Sanity: refuse to nuke a directory that doesn't look like ours.
    haz_bin = target / "venv" / "bin" / "haz"
    repo_dir = target / "repo"
    if not haz_bin.is_file() or not repo_dir.is_dir():
        click.echo(
            f"refusing to uninstall: {target} doesn't look like a hyperagent0 "
            f"install (expected {haz_bin} and {repo_dir}).",
            err=True,
        )
        sys.exit(2)

    # Build the action list so we can show the user exactly what changes.
    venv_dir = target / "venv"
    logs_dir = target / "logs"

    paths_to_remove: list[tuple[Path, str]] = [(venv_dir, "venv")]
    if not keep_state:
        paths_to_remove.append((repo_dir, "repo (source clone, project data, settings)"))
        paths_to_remove.append((logs_dir, "logs"))
    else:
        # Repo's usr/ keeps settings + projects. Remove .git, hyperagent0/,
        # python/, agent.py et al — keep only repo/usr/ as the surviving
        # state pocket.
        click.echo("  --keep-state: repo/usr/ and logs/ will be preserved.")

    # Symlinks that point into target's venv.
    relevant_links = [l for l in _bin_links() if _resolves_into(l, target)]

    # Summary.
    click.echo(f"hyperagent0 uninstall — prefix: {target}")
    for path, label in paths_to_remove:
        if path.exists():
            click.echo(f"  remove  {path}  ({_fmt_size(_dir_size(path))})  [{label}]")
    for link in relevant_links:
        click.echo(f"  unlink  {link}")
    if keep_state and (target / "repo" / "usr").exists():
        click.echo(f"  keep    {target / 'repo' / 'usr'}  (state)")
    if keep_state and logs_dir.exists():
        click.echo(f"  keep    {logs_dir}")

    if not yes:
        click.confirm("Proceed?", abort=True)

    # Stop the daemon if it's running.
    if _daemon.is_running():
        pid = _daemon.get_pid()
        click.echo(f"stopping running daemon (PID {pid})...")
        try:
            _daemon.send_signal()
            _daemon.wait_for_exit(timeout=30.0)
        except Exception as exc:
            click.echo(f"warning: failed to stop daemon cleanly: {exc}", err=True)

    # Do the removals.
    for path, _label in paths_to_remove:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    if keep_state:
        # In --keep-state mode we removed venv/ but kept repo/. Remove
        # everything under repo/ EXCEPT usr/, so a future re-install
        # gets a clean checkout but the user's projects persist.
        if repo_dir.is_dir():
            for child in repo_dir.iterdir():
                if child.name == "usr":
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass

    for link in relevant_links:
        try:
            link.unlink()
        except OSError:
            pass

    # Remove the now-empty prefix dir if --keep-state didn't leave anything.
    if not keep_state and target.exists():
        try:
            # Only rmdir if truly empty (don't blow away unrelated files).
            if not any(target.iterdir()):
                target.rmdir()
        except OSError:
            pass

    click.echo("uninstall complete.")
    if not keep_state:
        click.echo(
            "To reinstall later: "
            "curl -fsSL https://raw.githubusercontent.com/kundeng/hyperagent-zero/v2-hyperagent/install.sh | bash"
        )
