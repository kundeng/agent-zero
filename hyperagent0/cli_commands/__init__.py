"""CLI command modules for hyperagent0.

Each module in this package exposes a single top-level Click object
named ``command``. The lazy group in :mod:`hyperagent0.cli` imports
these modules on demand so heavy dependencies (Flask, Docker, LiteLLM,
channel SDKs) stay out of the cold-start path for fast commands like
``status``, ``stop``, and ``logs``.
"""
