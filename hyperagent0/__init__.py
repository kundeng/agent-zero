"""hyperagent0 — top-level package for the hyperagent-zero CLI and daemon.

The wrapper package intentionally exposes only a tiny public surface:
the Click entry point (``main``) and the package version. All heavy
imports (Flask, LiteLLM, channel SDKs, Docker, etc.) live inside the
``cli_commands.*`` submodules and are loaded lazily on demand so the
non-launch CLI commands (``--help``, ``status``, ``stop``, ``logs``)
stay within their cold-start budget (spec 03 D5).
"""

__version__ = "0.1.0"

# NOTE: do NOT import .cli here. The entry-point binding in pyproject.toml
# (``hyperagent0.cli:main``) imports the module directly, and importing it
# eagerly here would defeat the lazy-loading guarantee for downstream
# tooling that does ``import hyperagent0`` for version checks etc.
