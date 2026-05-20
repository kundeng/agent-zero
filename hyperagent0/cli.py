"""HyperAgent Zero CLI entry point.

Spec 01-host-first commits only to the packaging contract: a single ``main``
function reachable from both the ``hyperagent0`` and ``haz`` console_scripts.
Spec 03-daemon-cli owns the full subcommand surface (``start``, ``stop``,
``status``, ``setup``, ...). This file ships a minimal stub so the entry
points resolve immediately after ``pip install -e .``.

Imports inside ``main`` must stay lazy — spec 03 will wire in Click and
load subcommand modules on demand to keep ``haz --help`` startup fast.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hyperagent0",
        description=(
            "HyperAgent Zero — host-first agentic harness. "
            "Subcommands are filled in by spec 03 (daemon-cli)."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hyperagent0 {__version__}",
    )
    # Spec 03 replaces this stub with a Click group + lazy subcommand loader.
    parser.add_argument(
        "command",
        nargs="?",
        help="(reserved for spec 03 — daemon CLI)",
    )
    parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="(reserved for spec 03 — daemon CLI)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)

    if ns.command is None:
        parser.print_help()
        return 0

    # Stub: spec 03 will dispatch to subcommand modules here.
    sys.stderr.write(
        f"hyperagent0: subcommand {ns.command!r} is not yet implemented "
        "(spec 03-daemon-cli).\n"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
