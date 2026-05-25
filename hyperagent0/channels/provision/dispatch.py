"""Glue layer that the Flask handlers and the CLI shim both call into.

The four ``/channels_*`` POST handlers (status/provision/oauth/test/
apply/bind) each map to a small dispatch helper here. Keeping the
plumbing in one module ensures:

* the Flask handlers stay one-liners (less surface to maintain), and
* the CLI shim can hit the same code path in-process when the daemon
  isn't running, without re-encoding any of the wiring.

The helpers below build a :class:`ProvisionContext` for each call,
resolve the right provisioner from the registry, and translate
exceptions into :class:`StepResult` shapes the UI can render.

Provisioner side-effects (HTTP calls, secrets writes) happen
synchronously from the dispatch's point of view — the Flask handler
is responsible for offloading the dispatch into a worker thread if
the platform call could block the event loop. See
:func:`run_step` for the wrapping seam.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import (
    BaseProvisioner,
    ProvisionContext,
    StepResult,
    get_provisioner,
    registered_provisioners,
)
from .sessions import SessionCache, default_cache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Built-in provisioner discovery
# ---------------------------------------------------------------------------


def ensure_provisioners_loaded() -> None:
    """Import every shipped provisioner module so the registry is populated.

    Idempotent. Each provisioner module's import side-effect is a
    :func:`register_provisioner` call — once that side-effect has
    fired, subsequent imports are no-ops.

    Adapter modules import lazily-bounded SDKs only inside ``connect()``;
    same here — provisioner modules use stdlib ``urllib.request`` so
    importing them is cheap. The dispatch layer calls this once on
    first use rather than at module import to honor the spec 03 D5
    cold-start budget for ``haz --help``.
    """

    for mod_path in _BUILTIN_PROVISIONER_MODULES:
        try:
            __import__(mod_path)
        except ModuleNotFoundError:
            # Provisioner module hasn't been written yet (during the
            # spec-08 build) or was intentionally stripped from a slim
            # install. The registry handles its absence gracefully —
            # ``/channels_status`` simply won't list the platform.
            logger.debug("provisioner module %s not present; skipping", mod_path)
        except Exception:  # pragma: no cover - defensive
            # Module exists but its top-level raised — that's a real
            # bug worth surfacing in the daemon log.
            logger.exception("failed to load provisioner module %s", mod_path)


_BUILTIN_PROVISIONER_MODULES: tuple[str, ...] = (
    "hyperagent0.channels.provision.slack",
    "hyperagent0.channels.provision.telegram",
    "hyperagent0.channels.provision.discord",
)


# ---------------------------------------------------------------------------
# Context construction
# ---------------------------------------------------------------------------


def make_context(
    channel_type: str,
    session_id: Optional[str],
    *,
    host_base_url: Optional[str] = None,
    cache: Optional[SessionCache] = None,
    bot_name: str = "",
) -> ProvisionContext:
    """Build a :class:`ProvisionContext` for one dispatch.

    Resolves (or mints) the session, instantiates the right bridges,
    and returns a fully wired context. Pass ``cache`` for test
    isolation; production callers omit it to get the module-wide
    singleton.

    ``bot_name`` (spec 09 task 1.13) seeds the per-bot secret-key
    allow-list. Empty / legacy names produce the bare keys; named
    bots produce ``KEY_<BOTNAME>`` so multi-bot installs don't
    collide. Provisioners read it back via :attr:`ctx.bot_name`.
    """

    cls = get_provisioner(channel_type)
    if cls is None:
        raise LookupError(
            f"no provisioner registered for channel_type={channel_type!r}; "
            f"registered: {registered_provisioners()}"
        )

    sessions = cache or default_cache()
    session = sessions.get_or_start(channel_type, session_id)

    # Bridges lazy-import their upstream/heavy deps inside their own
    # methods, so building them is cheap.
    from ..channels_config_bridge import FileChannelsConfigBridge
    from ..secrets_bridge import AllowlistedSecretsBridge

    return ProvisionContext(
        channel_type=channel_type,
        session_id=session.session_id,
        session=session.scratch,
        secrets=AllowlistedSecretsBridge(cls.required_secrets, bot_name=bot_name),
        channels_config=FileChannelsConfigBridge(),
        host_base_url=host_base_url,
        bot_name=bot_name,
    )


def get_provisioner_instance(channel_type: str) -> BaseProvisioner:
    """Resolve and instantiate the registered provisioner.

    A fresh instance per dispatch is fine — provisioners are
    stateless (state lives in :class:`ProvisionContext`).
    """

    cls = get_provisioner(channel_type)
    if cls is None:
        raise LookupError(f"no provisioner registered for {channel_type!r}")
    return cls()


# ---------------------------------------------------------------------------
# Dispatch helpers — Flask handlers + CLI both call these
# ---------------------------------------------------------------------------


def run_step(
    channel_type: str,
    step_id: str,
    inputs: dict[str, Any],
    *,
    session_id: Optional[str] = None,
    host_base_url: Optional[str] = None,
    cache: Optional[SessionCache] = None,
) -> tuple[ProvisionContext, StepResult]:
    """Execute ``provisioner.provision(step_id, inputs, ctx)``.

    Returns ``(ctx, result)`` so callers can pull the session id back
    out of the context for the next round-trip. Exceptions are caught
    and translated to :class:`StepResult` error shapes — that gives
    the UI one consistent error path instead of one for HTTP 500s and
    another for in-band validation failures.

    Spec 09 task 1.13: the dispatch sniffs ``inputs["bot_name"]`` (set
    by the wizard's first step) and falls back to a previously stashed
    ``session.scratch["bot_name"]`` so subsequent steps inherit the
    same bot identity. The resolved value seeds both the per-bot
    secret bridge and :attr:`ctx.bot_name`.
    """

    ensure_provisioners_loaded()

    # Resolve bot_name BEFORE building the context so the secret
    # bridge's allow-list matches the keys the provisioner will write.
    sessions = cache or default_cache()
    session = sessions.get_or_start(channel_type, session_id)
    bot_name = (
        str(inputs.get("bot_name") or "").strip()
        or str(session.scratch.get("bot_name") or "").strip()
    )
    # Persist so future steps in this session don't need to resend it.
    if bot_name:
        session.scratch["bot_name"] = bot_name

    ctx = make_context(
        channel_type,
        session.session_id,
        host_base_url=host_base_url,
        cache=cache,
        bot_name=bot_name,
    )
    provisioner = get_provisioner_instance(channel_type)
    try:
        result = provisioner.provision(step_id, inputs, ctx)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "provisioner %s step %s raised; surfacing as StepResult.error",
            channel_type,
            step_id,
        )
        result = StepResult(error=str(exc))
    return ctx, result


def run_oauth_callback(
    channel_type: str,
    query: dict[str, Any],
    *,
    session_id: Optional[str] = None,
    host_base_url: Optional[str] = None,
    cache: Optional[SessionCache] = None,
) -> tuple[ProvisionContext, StepResult]:
    """Execute ``provisioner.oauth_callback(query, ctx)``.

    Same shape as :func:`run_step`. Callers (the OAuth callback
    handler in particular) re-render the result into an HTML page
    that posts back to ``window.opener``.
    """

    ensure_provisioners_loaded()
    ctx = make_context(
        channel_type, session_id, host_base_url=host_base_url, cache=cache
    )
    provisioner = get_provisioner_instance(channel_type)
    try:
        result = provisioner.oauth_callback(query, ctx)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "provisioner %s oauth_callback raised; surfacing as StepResult.error",
            channel_type,
        )
        result = StepResult(error=str(exc))
    return ctx, result


def run_test_connection(
    channel_type: str,
    *,
    session_id: Optional[str] = None,
    cache: Optional[SessionCache] = None,
) -> tuple[ProvisionContext, str]:
    """Execute ``provisioner.test_connection(ctx)``.

    Returns the human-facing message. The handler wraps any exception
    in a 500 — for ``test_connection`` we deliberately do *not*
    translate to a :class:`StepResult` because there's no recovery
    path the user can take in-band.
    """

    ensure_provisioners_loaded()
    ctx = make_context(channel_type, session_id, cache=cache)
    provisioner = get_provisioner_instance(channel_type)
    return ctx, provisioner.test_connection(ctx)


def wizard_steps_for(channel_type: str) -> list[dict[str, Any]]:
    """Return the provisioner's :meth:`wizard_steps` as JSON-shaped dicts.

    Pure read — no context, no session. The Flask handler hands the
    list straight to the browser.
    """

    ensure_provisioners_loaded()
    provisioner = get_provisioner_instance(channel_type)
    return [step.to_json() for step in provisioner.wizard_steps()]


def list_provisioners() -> list[dict[str, Any]]:
    """Return a JSON-shaped list of registered provisioners.

    Used by both ``/channels_status`` (to enumerate platforms even
    before any are configured) and the ``haz channel list`` CLI.
    """

    ensure_provisioners_loaded()
    out: list[dict[str, Any]] = []
    for name in registered_provisioners():
        cls = get_provisioner(name)
        if cls is None:
            continue
        out.append(
            {
                "channel_type": name,
                "required_secrets": list(cls.required_secrets),
                "bootstrap_url": cls.bootstrap_url,
            }
        )
    return out
