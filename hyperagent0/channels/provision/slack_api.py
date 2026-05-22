"""Slack HTTP wrappers used by :class:`SlackProvisioner` (spec 08 task 1.16).

stdlib ``urllib.request`` only — no ``requests`` / ``slack_sdk``
dependencies. Provisioning code paths must be installable on a
minimal host that hasn't pulled the runtime ``[slack]`` extra yet.

Each function returns the parsed JSON body on success and raises
:class:`SlackApiError` (or one of its subclasses) on failure. We
distinguish three failure modes the provisioner needs to react to
differently:

* :class:`SlackTokenExpiredError` — the config-access token has
  aged out. The provisioner retries once after calling
  :func:`rotate_config_token` with the refresh token.
* :class:`SlackManifestError` — Slack rejected the manifest. The
  detail carries the ``errors[].pointer`` / ``errors[].message``
  pairs so the UI can highlight the offending field.
* :class:`SlackApiError` — anything else (auth, network, generic
  ``ok: false``).

The endpoints used here are:

* ``apps.manifest.create`` — register the app from the manifest.
* ``tooling.tokens.rotate`` — refresh the config-access token.
* ``oauth.v2.access`` — exchange the OAuth code for a bot token.
* ``apps.connections.open`` — Best-effort attempt to mint an
  app-level token via the config-access token. Slack's docs do not
  promise this works for all workspaces; the provisioner treats
  failure as "fall back to paste".
* ``chat.postMessage`` — used for :meth:`test_connection`.
* ``auth.test`` — used to resolve the bot user id at test time.

All POSTs use ``Content-Type: application/json``. The Slack docs
permit ``application/x-www-form-urlencoded`` for the legacy
endpoints, but the JSON path is supported across all the endpoints
we touch and saves us a query-string encoder.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)


_SLACK_API_BASE = "https://slack.com/api"
_DEFAULT_TIMEOUT_S = 15.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SlackApiError(RuntimeError):
    """Slack returned ``ok: false`` or HTTP transport raised.

    ``code`` is the Slack ``error`` field when available, or
    ``"http_<status>"`` for HTTP-level failures.
    """

    def __init__(self, message: str, *, code: str = "", payload: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class SlackTokenExpiredError(SlackApiError):
    """Config-access token has expired and needs rotation.

    Slack signals this via ``error == "token_expired"`` (and a few
    closely related codes). The provisioner catches this specifically
    and attempts a refresh-token rotation before retrying.
    """


class SlackManifestError(SlackApiError):
    """``apps.manifest.create`` rejected the manifest.

    ``manifest_errors`` is the list of ``{"pointer": ..., "message": ...}``
    pairs Slack returns under ``response_metadata.messages`` /
    ``errors[]``. The UI uses them to point users at the offending
    field.
    """

    def __init__(
        self,
        message: str,
        *,
        manifest_errors: list[dict[str, Any]],
        payload: Optional[dict] = None,
    ):
        super().__init__(message, code="invalid_manifest", payload=payload)
        self.manifest_errors = manifest_errors


# ---------------------------------------------------------------------------
# Low-level POST helper
# ---------------------------------------------------------------------------


def _post_json(
    endpoint: str,
    body: dict[str, Any],
    *,
    bearer: Optional[str] = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """POST ``body`` as JSON to ``<slack-api-base>/<endpoint>``.

    Returns the parsed response. Raises :class:`SlackApiError` if the
    HTTP layer fails or Slack returns ``ok: false``.

    Test seam: monkey-patch :func:`urllib.request.urlopen` to feed
    canned responses. We deliberately use the module-level
    ``urllib.request`` name so tests don't have to peek into our
    internals.
    """

    url = f"{_SLACK_API_BASE}/{endpoint}"
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # Slack sometimes returns 200 with ok:false (the normal
        # in-band error path) and sometimes legit 4xx/5xx (transport
        # errors). Treat 4xx/5xx as transport.
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            pass
        raise SlackApiError(
            f"HTTP {exc.code} from {endpoint}: {body_text[:200]}",
            code=f"http_{exc.code}",
        ) from exc
    except urllib.error.URLError as exc:
        raise SlackApiError(
            f"network error contacting {endpoint}: {exc.reason}",
            code="network_error",
        ) from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SlackApiError(
            f"non-JSON response from {endpoint}: {raw[:200]!r}",
            code="invalid_response",
        ) from exc

    if not isinstance(payload, dict) or "ok" not in payload:
        raise SlackApiError(
            f"unexpected response shape from {endpoint}: {payload!r}",
            code="invalid_response",
            payload=payload if isinstance(payload, dict) else None,
        )

    if not payload.get("ok"):
        return _raise_for_error(endpoint, payload)

    return payload


def _raise_for_error(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Translate a Slack ``ok: false`` response into a typed exception."""

    code = str(payload.get("error", "unknown_error"))

    # Token expired family — exact strings Slack uses vary across
    # docs; capture the common ones. Provisioner only needs to know
    # "should I rotate and retry".
    if code in {
        "token_expired",
        "tokens_expired",
        "invalid_token",
        "not_authed",
    }:
        raise SlackTokenExpiredError(
            f"{endpoint}: {code}", code=code, payload=payload
        )

    if code == "invalid_manifest":
        manifest_errors: list[dict[str, Any]] = []
        for entry in payload.get("errors") or []:
            if isinstance(entry, dict):
                manifest_errors.append(
                    {
                        "pointer": entry.get("pointer"),
                        "message": entry.get("message"),
                    }
                )
        raise SlackManifestError(
            f"{endpoint}: invalid_manifest — {len(manifest_errors)} error(s)",
            manifest_errors=manifest_errors,
            payload=payload,
        )

    raise SlackApiError(
        f"{endpoint}: {code}", code=code, payload=payload
    )


# ---------------------------------------------------------------------------
# Endpoint wrappers
# ---------------------------------------------------------------------------


def create_app_from_manifest(
    config_token: str, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Call ``apps.manifest.create``.

    The manifest field is JSON-encoded as a string per the API
    contract. Returns the parsed response containing ``app_id``,
    ``credentials`` (signing_secret, client_id, client_secret,
    verification_token), and ``oauth_authorize_url``.
    """

    return _post_json(
        "apps.manifest.create",
        {"manifest": json.dumps(manifest)},
        bearer=config_token,
    )


def rotate_config_token(refresh_token: str) -> dict[str, Any]:
    """Call ``tooling.tokens.rotate`` to mint a fresh config-access token.

    Returns the parsed response. ``token`` is the new access token;
    ``refresh_token`` is the new refresh token (rotation is single-
    use). Provisioner is responsible for using the new refresh token
    on subsequent rotations.
    """

    return _post_json("tooling.tokens.rotate", {"refresh_token": refresh_token})


def exchange_oauth_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Call ``oauth.v2.access`` to swap the install code for a bot token.

    Returns the parsed response containing ``access_token`` (the
    ``xoxb-…`` bot token), ``app_id``, ``team`` (with ``id`` and
    ``name``), and ``authed_user``.

    Unlike most Slack endpoints, this one accepts form-encoded body
    with credentials as form parameters rather than a bearer header.
    """

    url = f"{_SLACK_API_BASE}/oauth.v2.access"
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise SlackApiError(
            f"HTTP {exc.code} from oauth.v2.access", code=f"http_{exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SlackApiError(
            f"network error contacting oauth.v2.access: {exc.reason}",
            code="network_error",
        ) from exc

    payload = json.loads(raw.decode("utf-8"))
    if not payload.get("ok"):
        return _raise_for_error("oauth.v2.access", payload)
    return payload


def try_mint_app_token(
    config_token: str, app_id: str, scopes: tuple[str, ...] = ("connections:write",)
) -> Optional[str]:
    """Attempt to mint an app-level ``xapp-…`` token via the config-access token.

    Returns the token string on success, or ``None`` if Slack rejects
    the call. We deliberately swallow errors here — paste-fallback is
    a first-class path in the wizard, so a failed mint is not an
    error condition.

    There is no stable public documentation for an endpoint that
    issues app-level tokens given a config-access token. We attempt
    the most-frequently-cited shape (``apps.connections.open`` with
    the ``app_id`` and scope) and fall back gracefully when it
    doesn't work. If Slack ever publishes a documented method,
    extend this wrapper to use it.
    """

    try:
        payload = _post_json(
            "apps.connections.open",
            {"app_id": app_id, "scopes": ",".join(scopes)},
            bearer=config_token,
        )
    except SlackApiError as exc:
        logger.info(
            "apps.connections.open declined to mint app-level token "
            "(code=%s); falling back to paste",
            exc.code,
        )
        return None

    # Slack returns the token under ``token`` on success. Defensive
    # against schema drift — look at a couple of likely fields.
    token = payload.get("token") or payload.get("app_token")
    return str(token) if token else None


def chat_post_message(
    bot_token: str, channel: str, text: str
) -> dict[str, Any]:
    """Call ``chat.postMessage`` — used by :meth:`SlackProvisioner.test_connection`.

    ``channel`` may be a channel id (``C0123…``) or a name with the
    ``#`` prefix (``#general``); Slack resolves both.
    """

    return _post_json(
        "chat.postMessage",
        {"channel": channel, "text": text},
        bearer=bot_token,
    )


def auth_test(bot_token: str) -> dict[str, Any]:
    """Call ``auth.test`` — used to resolve the bot's identity for the smoke test.

    Returns ``{"ok": true, "user": "...", "user_id": "...", "team":
    "...", "team_id": "...", ...}``.
    """

    return _post_json("auth.test", {}, bearer=bot_token)
