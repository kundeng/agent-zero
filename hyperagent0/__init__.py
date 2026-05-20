"""HyperAgent Zero — host-first agentic harness.

Per spec 01-host-first (D9), this top-level package contains all net-new
code while the upstream-mirrored ``python/`` tree stays unrenamed to
preserve cherry-pick ergonomics.

Imports here are intentionally lazy: importing :mod:`hyperagent0` must not
pull in Flask, LiteLLM, Anthropic SDK, or any channel adapter. Heavy modules
are loaded on demand inside submodules and CLI subcommands.

NOTE: do NOT import :mod:`.cli` from this module. The entry-point binding
in ``pyproject.toml`` (``hyperagent0.cli:main``) imports the module directly,
and importing it eagerly here would defeat the lazy-loading guarantee for
downstream tooling that does ``import hyperagent0`` for version checks etc.
"""

__version__ = "0.1.0.dev0"
__all__ = ["__version__"]
