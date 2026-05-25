"""Generic channel-provisioner contract (spec 08, D1+D2+D5+D6).

A *provisioner* is the per-platform class that knows how to take a
small bundle of operator-supplied inputs (typically: one bootstrap
credential plus a few clicks) and produce a working channel
configuration — tokens written to ``usr/secrets.env`` and a per-channel
block dropped into ``~/.hyperagent0/channels.json`` that references
those tokens via ``$$secret(NAME)`` placeholders.

The contract is deliberately narrow:

* :meth:`BaseProvisioner.wizard_steps` returns a declarative list of
  :class:`WizardStep` descriptors. The Web UI renders those steps in
  the Channels tab wizard; the ``haz channel provision`` CLI introspects
  them to derive ``--<field-id>`` flags. The same descriptor list
  drives both surfaces — there is no platform-specific UI or CLI code.

* :meth:`BaseProvisioner.provision` is called once per step. It runs
  the platform's HTTP calls, writes secrets via the
  :class:`SecretsBridge`, updates the channel block via the
  :class:`ChannelsConfigBridge`, and returns a :class:`StepResult`
  that either advances to the next step or terminates the flow.

* :meth:`BaseProvisioner.oauth_callback` is the entry point the
  generic ``/oauth/<channel_type>/callback`` route dispatches to —
  only platforms with browser OAuth (e.g. Slack) override the default
  ``not_supported`` shape.

* :meth:`BaseProvisioner.test_connection` performs the post-provision
  smoke check (e.g. ``chat.postMessage`` for Slack, ``getMe`` for
  Telegram). UI shows the result; CLI prints it.

* :meth:`BaseProvisioner.channels_json_block` returns the exact dict
  to drop into ``channels.json`` once provisioning is complete. It
  references secrets only via placeholders so the JSON file can be
  committed / backed up without leaking tokens.

The framework intentionally keeps no global state besides the
registry and per-session scratch space. Every provisioner instance
gets fresh dataclasses and a fresh :class:`ProvisionContext`.
"""

from __future__ import annotations

import abc
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol


# ---------------------------------------------------------------------------
# Wizard step descriptors (D2)
# ---------------------------------------------------------------------------


WizardStepKind = Literal[
    "input",
    "link_with_callback",
    "link_with_paste",
    "info",
    "summary",
]
"""Step kinds. See spec 08 D2 for what each kind means to the UI/CLI."""


WizardFieldKind = Literal["text", "password", "select", "checkbox", "textarea"]


@dataclass
class WizardField:
    """A single input field inside an ``input`` (or paste) step."""

    id: str
    label: str
    kind: WizardFieldKind = "text"
    placeholder: str = ""
    help_text: str = ""
    required: bool = True
    secret: bool = False
    options: list[dict[str, str]] = field(default_factory=list)  # for kind="select"
    default: Optional[str] = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "placeholder": self.placeholder,
            "help_text": self.help_text,
            "required": self.required,
            "secret": self.secret,
            "options": list(self.options),
            "default": self.default,
        }


@dataclass
class WizardStep:
    """One step in a provisioner's wizard.

    The same descriptor list is consumed by:

    * the Alpine renderer at
      ``webui/components/settings/channels/wizard.html``, and
    * the ``haz channel provision`` CLI which derives flags from
      ``fields[]``.
    """

    id: str
    kind: WizardStepKind
    label: str
    help_text: str = ""
    fields: list[WizardField] = field(default_factory=list)
    #: URL the UI should open in a popup (for ``link_with_callback`` /
    #: ``link_with_paste``). Provisioners may set this dynamically by
    #: returning a :class:`StepResult` whose ``next_step`` carries the
    #: resolved URL.
    url: Optional[str] = None
    #: Seconds to wait for the OAuth callback before falling back
    #: (``link_with_callback`` only).
    timeout_s: int = 90
    #: Optional next-step hints. ``None`` means "terminal step".
    next_on_success: Optional[str] = None
    next_on_timeout: Optional[str] = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "help_text": self.help_text,
            "fields": [f.to_json() for f in self.fields],
            "url": self.url,
            "timeout_s": self.timeout_s,
            "next_on_success": self.next_on_success,
            "next_on_timeout": self.next_on_timeout,
        }


@dataclass
class StepResult:
    """Return shape of every :meth:`BaseProvisioner.provision` call.

    Three terminal outcomes:

    * ``terminal=True`` — provisioning is complete. The UI/CLI shows
      ``message`` and applies the channel.
    * ``next_step != None`` — render the named step next. Optional
      ``url`` override (used to inject the install URL into the
      callback step). Optional ``state_token`` for CSRF on OAuth.
    * ``error != None`` — surfaces a user-facing error. ``error_pointer``
      (e.g. ``/oauth_config/scopes``) points at the offending field
      when the platform supplies one.
    """

    next_step: Optional[str] = None
    terminal: bool = False
    message: str = ""
    url_override: Optional[str] = None
    state_token: Optional[str] = None
    error: Optional[str] = None
    error_pointer: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "next_step": self.next_step,
            "terminal": self.terminal,
            "message": self.message,
            "url_override": self.url_override,
            "state_token": self.state_token,
            "error": self.error,
            "error_pointer": self.error_pointer,
            "extra": dict(self.extra),
        }


def step_result_to_json(result: StepResult) -> str:
    """Serialize a :class:`StepResult` to a JSON string.

    Exposed at the package boundary so Flask handlers don't have to
    know the internal shape.
    """

    return json.dumps(result.to_json())


# ---------------------------------------------------------------------------
# Side-effect bridges (D5+D6)
# ---------------------------------------------------------------------------


class SecretsBridge(Protocol):
    """Write-only API the provisioner uses to update ``usr/secrets.env``.

    Implementations validate that the keys being written are listed in
    the provisioner's ``required_secrets`` allow-list — a write to an
    undeclared key is a programming error, not an operator mistake.

    Single atomic merge per call (the underlying
    ``SecretsManager.save_secrets_with_merge`` handles dedup, comment
    preservation, and ordering).
    """

    def write(self, values: dict[str, str]) -> None:
        ...

    def read(self, key: str) -> Optional[str]:
        """Return the cleartext value, or ``None`` if not set.

        Provisioners use this sparingly — usually only to confirm that
        a prior step's secret survived (e.g. picking up
        ``SLACK_CLIENT_SECRET`` from step 1 inside the OAuth callback).
        """


class ChannelsConfigBridge(Protocol):
    """Read/write API for ``~/.hyperagent0/channels.json``.

    Provisioners call :meth:`update_block` to merge their per-platform
    block; the bridge handles atomic-rename writes so a half-written
    file never lands on disk.
    """

    def read_block(self, channel_type: str) -> dict[str, Any]:
        ...

    def update_block(self, channel_type: str, block: dict[str, Any]) -> None:
        ...

    def update_project_binding(
        self,
        channel_type: str,
        *,
        chat_id: Optional[str],
        project_name: Optional[str],
    ) -> None:
        ...


# ---------------------------------------------------------------------------
# Context passed to every provisioner method (D5)
# ---------------------------------------------------------------------------


@dataclass
class ProvisionContext:
    """Cross-step state container.

    A new context is created at the start of each provisioning session
    and reused across all of that session's steps. Provisioners stash
    intermediate values (e.g. the ``app_id`` from step 1) in
    :attr:`session` so later steps can pick them up without
    round-tripping them through the UI.

    ``session`` is a plain dict — provisioners own the keys they put
    in it. The session cache (see ``sessions.py``) handles TTL
    eviction; provisioners do not.
    """

    channel_type: str
    session_id: str
    session: dict[str, Any]
    secrets: SecretsBridge
    channels_config: ChannelsConfigBridge
    #: Filled in by the Flask handler when running inside the daemon
    #: web server. ``None`` when the CLI runs provisioning in-process.
    #: Provisioners that need to compose redirect URLs read this.
    host_base_url: Optional[str] = None
    #: Spec 09 D5: which bot this provisioning session is targeting.
    #: Set by :func:`dispatch.make_context` from the first step's
    #: ``bot_name`` input (and persisted into ``session.scratch`` so
    #: later steps recover it). Empty for legacy callers that never
    #: collected a bot name — secret keys then stay bare.
    bot_name: str = ""

    def new_state_token(self) -> str:
        """Mint a per-step CSRF token for OAuth ``state`` parameters.

        Stored in :attr:`session` under ``__state_tokens`` so the
        callback handler can verify it.
        """

        token = uuid.uuid4().hex
        tokens: dict[str, float] = self.session.setdefault("__state_tokens", {})
        # Track issue time; the OAuth callback verifies AND removes on use.
        tokens[token] = time.time()
        return token

    def consume_state_token(self, token: str, *, max_age_s: int = 600) -> bool:
        tokens: dict[str, float] = self.session.get("__state_tokens", {})
        issued = tokens.pop(token, None)
        if issued is None:
            return False
        return (time.time() - issued) <= max_age_s


# ---------------------------------------------------------------------------
# Provisioner ABC (D1)
# ---------------------------------------------------------------------------


class BaseProvisioner(abc.ABC):
    """Abstract per-platform provisioner.

    Subclasses declare three class attributes and implement five
    methods. The framework requires nothing else — keeping the surface
    this small is what lets new platforms drop in as a single file.

    Class attributes
    ----------------
    :attr:`channel_type`
        Registry key, matching the runtime adapter's ``channel_type``
        in :mod:`hyperagent0.channels.<platform>` (e.g. ``"slack"``).
    :attr:`required_secrets`
        The full list of secret keys this provisioner may write. Any
        write to a key not in this list via the :class:`SecretsBridge`
        raises — keeps the secret namespace explicit.
    :attr:`bootstrap_url`
        Optional URL the UI links to as "where to get the entry
        credential" (Slack: https://api.slack.com/apps; Telegram:
        https://t.me/BotFather). ``None`` for platforms with no
        well-defined external bootstrap page.
    """

    channel_type: str = ""
    required_secrets: list[str] = []
    bootstrap_url: Optional[str] = None

    @abc.abstractmethod
    def wizard_steps(self) -> list[WizardStep]:
        """Return the declarative step list (D2).

        Called by ``/channels_wizard/<channel_type>`` and by the CLI
        flag introspector. Must be a pure function — same inputs
        always produce the same list (no I/O, no platform calls).
        """

    @abc.abstractmethod
    def provision(
        self,
        step_id: str,
        inputs: dict[str, Any],
        ctx: ProvisionContext,
    ) -> StepResult:
        """Execute one wizard step.

        Implementations should:

        * Validate ``inputs`` against the corresponding
          :class:`WizardStep`'s ``fields[]``.
        * Call the platform's HTTP APIs as needed.
        * Persist intermediate values via ``ctx.session`` (for use by
          later steps) and final values via ``ctx.secrets``.
        * Return a :class:`StepResult` indicating the next step or
          terminal completion.

        Slow HTTP calls run inside the executor the Flask handler
        wraps the dispatch in — provisioner code itself stays
        synchronous (cleaner control flow, easier testing).
        """

    @abc.abstractmethod
    def oauth_callback(
        self,
        query: dict[str, Any],
        ctx: ProvisionContext,
    ) -> StepResult:
        """Handle ``GET /oauth/<channel_type>/callback``.

        Platforms without browser OAuth return a fixed "not supported"
        StepResult here. Implementations that DO use OAuth typically:

        1. Verify ``query["state"]`` via
           :meth:`ProvisionContext.consume_state_token`.
        2. Exchange ``query["code"]`` for tokens via the platform's
           ``oauth.v2.access`` (or equivalent).
        3. Write the resulting tokens through ``ctx.secrets``.
        4. Return a StepResult that advances the wizard.
        """

    @abc.abstractmethod
    def test_connection(self, ctx: ProvisionContext) -> str:
        """Smoke-test the configured channel.

        Returns a user-facing one-liner ("connected as @hyperagent
        in workspace acme") or raises with a clear message on failure.
        """

    @abc.abstractmethod
    def channels_json_block(self, ctx: ProvisionContext) -> dict[str, Any]:
        """Produce the dict to merge into ``channels.json[channel_type]``.

        Must reference token-shaped values only as ``$$secret(KEY)``
        placeholders — never raw. Non-secret fields (``enabled``,
        ``require_mention``, ``project_binding``, ``allowed_users``,
        ``allowed_chats``) pass through unchanged.
        """


# ---------------------------------------------------------------------------
# Registry (mirrors hyperagent0.channels.base._REGISTRY)
# ---------------------------------------------------------------------------


_PROVISIONER_REGISTRY: dict[str, type[BaseProvisioner]] = {}


def register_provisioner(name: str, cls: type[BaseProvisioner]) -> None:
    """Register ``cls`` as the provisioner for ``name`` (its ``channel_type``).

    Each per-platform module calls this at import time. Re-registering
    the same name is allowed and replaces the prior entry (useful when
    tests register a stub provisioner over the real one).
    """

    if not name:
        raise ValueError("provisioner name must be non-empty")
    if not isinstance(name, str):
        raise TypeError("provisioner name must be a string")
    _PROVISIONER_REGISTRY[name] = cls


def get_provisioner(name: str) -> Optional[type[BaseProvisioner]]:
    """Return the registered provisioner class for ``name``, or ``None``.

    Flask handlers and the CLI both go through this — they never
    import per-platform modules directly. Per-platform modules are
    imported by the lifecycle bootstrap (see
    ``hyperagent0/channels/provision/lifecycle.py``) so their
    ``register_provisioner`` side-effect fires before any HTTP request
    or CLI invocation hits the registry.
    """

    return _PROVISIONER_REGISTRY.get(name)


def registered_provisioners() -> list[str]:
    """Return the sorted list of registered ``channel_type`` keys."""

    return sorted(_PROVISIONER_REGISTRY.keys())
