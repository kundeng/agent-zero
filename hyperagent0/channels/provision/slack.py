"""Slack provisioner — first concrete :class:`BaseProvisioner` (spec 08 task 1.14).

Walks the operator through the four-step flow:

1. **Enter config-access token** — the workspace-level
   ``xoxe.xoxp-…`` minted at https://api.slack.com/apps. Optionally a
   refresh token for the once-per-call auto-rotation path.
   Provisioner POSTs ``apps.manifest.create`` with the manifest built
   by :func:`build_slack_manifest`. On success the response carries
   ``app_id``, the four ``credentials`` (signing secret, client id,
   client secret, verification token), and the workspace install URL.
2. **Install link + OAuth callback** — the wizard shows the install
   URL. The user clicks "Allow" in their browser. Slack redirects to
   ``/channels_oauth_callback?channel_type=slack&code=…&state=…``;
   :meth:`oauth_callback` swaps the code for a bot token
   (``xoxb-…``) via ``oauth.v2.access`` and writes it to secrets.
   Provisioner then *attempts* to mint the Socket-Mode app-level
   token via :func:`try_mint_app_token`. On success, jumps to the
   terminal summary; on decline, advances to step 3.
3. **App-level token paste** — link to
   ``api.slack.com/apps/<app_id>/general#app_level_tokens`` plus a
   paste field. User generates a ``connections:write`` token in the
   Slack UI and pastes it in. Provisioner writes it to secrets,
   flips ``channels.json`` ``slack.enabled=true``.
4. **Summary** — terminal step. The wizard shows what was
   configured; the user clicks "Apply" to trigger
   ``/channels_apply`` which restarts the channel runtime.

All Slack HTTP traffic goes through :mod:`hyperagent0.channels.provision.slack_api`.
The provisioner itself stays pure synchronous logic — easy to test
by monkeypatching that module's ``urlopen``.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import (
    BaseProvisioner,
    ProvisionContext,
    StepResult,
    WizardField,
    WizardStep,
    register_provisioner,
)
from .slack_api import (
    SlackApiError,
    SlackManifestError,
    SlackTokenExpiredError,
    auth_test,
    chat_post_message,
    config_token_team_info,
    create_app_from_manifest,
    exchange_oauth_code,
    rotate_config_token,
    try_mint_app_token,
)
from .slack_manifest import build_slack_manifest

logger = logging.getLogger(__name__)


# Session-scratch keys — kept module-private so the names don't leak
# into the UI/CLI surface.
_K_APP_ID = "app_id"
_K_CLIENT_ID = "client_id"
_K_CLIENT_SECRET = "client_secret"
_K_REDIRECT_URL = "redirect_url"
_K_INSTALL_URL = "install_url"
_K_REFRESH_TOKEN = "refresh_token"  # in-memory only; never persisted
_K_OAUTH_DONE = "oauth_done"
_K_APP_TOKEN_DONE = "app_token_done"
_K_MANIFEST_JSON = "manifest_json"  # D10: stashed for inclusion in step-2 message
_K_DISPLAY_NAME = "display_name"


class SlackProvisioner(BaseProvisioner):
    """End-to-end Slack channel provisioning via the manifest API."""

    channel_type = "slack"
    required_secrets = [
        "SLACK_APP_ID",
        "SLACK_TEAM_ID",
        "SLACK_SIGNING_SECRET",
        "SLACK_CLIENT_ID",
        "SLACK_CLIENT_SECRET",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
    ]
    bootstrap_url = "https://api.slack.com/apps"

    # ------------------------------------------------------------------
    # Wizard description (D2)
    # ------------------------------------------------------------------

    def wizard_steps(self) -> list[WizardStep]:
        """Spec 08 D10 wizard — paste-manifest, no orphan apps.

        Previously the wizard called ``apps.manifest.create`` (D3 path)
        which leaves orphan apps for non-distributable workspaces (the
        common case — see project memory ``project_slack_install_models``).
        D10 replaces that with the manual paste-manifest flow that's
        actually supported by Slack for any workspace.

        The old API-creation path is still callable from ``provision()``
        via the ``config_token`` step id, for the rare distributable-app
        case; the wizard simply doesn't surface it.
        """

        return [
            WizardStep(
                id="manifest_config",
                kind="input",
                label="Configure your Slack bot",
                help_text=(
                    "We'll generate a Slack app manifest you can paste "
                    "into Slack's UI. No 'config access token' from "
                    "Slack needed — the app gets created in your "
                    "developer account via the standard Create-App UI."
                ),
                fields=[
                    WizardField(
                        id="bot_name",
                        label="Bot name (local identifier)",
                        kind="text",
                        placeholder="default",
                        default="default",
                        required=True,
                        help_text=(
                            "Used in logs, channels.json, and per-bot "
                            "secret keys. Pick something unique within "
                            "this haz install if you plan to run more "
                            "than one Slack bot."
                        ),
                    ),
                    WizardField(
                        id="display_name",
                        label="Bot display name (in Slack)",
                        kind="text",
                        placeholder="hyperagent",
                        default="hyperagent",
                        help_text=(
                            "Shown in Slack as @<display_name>. "
                            "Lowercase, no spaces."
                        ),
                    ),
                    WizardField(
                        id="include_private_channels",
                        label="Support private channels",
                        kind="checkbox",
                        default="true",
                        required=False,
                    ),
                    WizardField(
                        id="include_dms",
                        label="Support direct messages",
                        kind="checkbox",
                        default="true",
                        required=False,
                    ),
                ],
                next_on_success="paste_bot_token",
            ),
            WizardStep(
                id="paste_bot_token",
                kind="link_with_paste",
                label="Create the Slack app from the manifest, then paste the bot token",
                help_text=(
                    "Instructions and the manifest JSON appear above "
                    "this form when you continue from step 1. Then:\n"
                    "1. Open https://api.slack.com/apps\n"
                    "2. Click 'Create New App' → 'From a manifest'\n"
                    "3. Pick your workspace, paste the JSON, click Next → Create\n"
                    "4. On the new app's page sidebar: 'Install App' → 'Install to Workspace' → Allow\n"
                    "5. On 'OAuth & Permissions', copy the Bot User OAuth Token (starts with xoxb-)\n"
                    "Paste it below."
                ),
                url="https://api.slack.com/apps",
                fields=[
                    WizardField(
                        id="bot_token",
                        label="Bot User OAuth Token (xoxb-...)",
                        kind="password",
                        secret=True,
                        required=True,
                    ),
                ],
                next_on_success="paste_app_token",
            ),
            WizardStep(
                id="paste_app_token",
                kind="link_with_paste",
                label="Generate the Socket Mode app-level token",
                help_text=(
                    "On the same app page in Slack: 'Basic Information' "
                    "→ scroll to 'App-Level Tokens' → click 'Generate "
                    "Token and Scopes' → name it (e.g. 'sockets'), add "
                    "scope 'connections:write' → Generate. Copy the "
                    "xapp- token and paste it below."
                ),
                url="https://api.slack.com/apps",
                fields=[
                    WizardField(
                        id="app_token",
                        label="App-level token (xapp-...)",
                        kind="password",
                        secret=True,
                        required=True,
                    ),
                ],
                next_on_success="summary",
            ),
            WizardStep(
                id="summary",
                kind="summary",
                label="Ready to apply",
                help_text=(
                    "Tokens are saved. Click Apply to start the Slack "
                    "channel adapter. The bot will respond to "
                    "@-mentions immediately."
                ),
            ),
        ]

    # ------------------------------------------------------------------
    # Step dispatch (D1)
    # ------------------------------------------------------------------

    def provision(
        self, step_id: str, inputs: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        # D10 default path (paste-manifest, no orphan apps):
        if step_id == "manifest_config":
            return self._step_manifest_config(inputs, ctx)
        if step_id == "paste_bot_token":
            return self._step_paste_bot_token_d10(inputs, ctx)
        if step_id == "paste_app_token":
            return self._step_app_token(inputs, ctx)
        # Legacy D3 path (kept callable for distributable-app workflows
        # but not surfaced by wizard_steps anymore):
        if step_id == "config_token":
            return self._step_config_token(inputs, ctx)
        if step_id == "install":
            install_url = ctx.session.get(_K_INSTALL_URL)
            if not install_url:
                return StepResult(
                    error="install URL missing from session; restart provisioning"
                )
            return StepResult(
                next_step="install",
                message="Click the link to install. Waiting for callback…",
                url_override=install_url,
            )
        if step_id == "install_paste_fallback":
            return self._step_paste_bot_token(inputs, ctx)
        if step_id == "app_token":
            return self._step_app_token(inputs, ctx)
        if step_id == "summary":
            return self._step_summary(ctx)
        return StepResult(error=f"unknown step {step_id!r}")

    # ------------------------------------------------------------------
    # D10 Step 1 — generate manifest, stash for step 2
    # ------------------------------------------------------------------

    def _step_manifest_config(
        self, inputs: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        """Generate the Slack app manifest from user inputs.

        D10 path: no Slack API call. The manifest JSON gets stashed in
        session state so step 2 can emit it as a copyable string. The
        operator pastes the JSON at https://api.slack.com/apps's
        "Create New App → From a manifest" form.

        ``ctx.bot_name`` is sourced upstream from ``inputs["bot_name"]``
        by ``dispatch.run_step`` — we don't read it directly here.
        """
        import json

        display_name = (
            str(inputs.get("display_name", "")).strip() or "hyperagent"
        )
        include_private = _truthy(inputs.get("include_private_channels", True))
        include_dms = _truthy(inputs.get("include_dms", True))

        # D10 manifest is identical to the D3 one MINUS the
        # oauth_config.redirect_urls (no OAuth callback in the paste flow).
        # We keep the helper that builds the full manifest and let Slack
        # ignore extra fields — simpler than threading a "no redirect"
        # toggle through build_slack_manifest.
        manifest = build_slack_manifest(
            display_name=display_name,
            # An unused but well-formed redirect URL keeps the manifest
            # schema-valid for Slack's parser even though the operator
            # never visits it in the D10 flow.
            redirect_url="https://localhost/unused-by-d10",
            include_private_channels=include_private,
            include_dms=include_dms,
        )
        manifest_json = json.dumps(manifest, indent=2)

        ctx.session[_K_MANIFEST_JSON] = manifest_json
        ctx.session[_K_DISPLAY_NAME] = display_name

        message = (
            "Copy the manifest JSON below, then continue:\n\n"
            "1. Open https://api.slack.com/apps and click 'Create New App'\n"
            "2. Choose 'From a manifest'\n"
            "3. Pick your workspace, paste the JSON, click Next → Create\n"
            "4. On the new app's page: sidebar → 'Install App' → "
            "'Install to Workspace' → Allow\n"
            "5. Go to 'OAuth & Permissions' and copy the "
            "'Bot User OAuth Token' (xoxb-...) — you'll paste it below.\n\n"
            "--- MANIFEST JSON ---\n"
            f"{manifest_json}\n"
            "--- END MANIFEST ---"
        )

        return StepResult(
            next_step="paste_bot_token",
            message=message,
            extra={
                "manifest_json": manifest_json,
                "display_name": display_name,
            },
        )

    def _step_paste_bot_token_d10(
        self, inputs: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        """Validate + persist the xoxb- bot token pasted from Slack.

        D10's step 2. Differs from the legacy ``_step_paste_bot_token``
        only by an upfront ``auth.test`` call: since this is the FIRST
        Slack-side action of the D10 flow, a typo'd token would otherwise
        only surface at adapter-start time. ``auth.test`` is a single
        HTTP round-trip — cheap insurance.
        """
        bot_token = str(inputs.get("bot_token", "")).strip()
        if not bot_token:
            return StepResult(
                error="bot_token is required", error_pointer="/bot_token"
            )
        if not bot_token.startswith("xoxb-"):
            return StepResult(
                error="Bot token must start with xoxb-",
                error_pointer="/bot_token",
            )

        # Validate so we don't store junk + advance.
        try:
            auth = auth_test(bot_token)
        except SlackApiError as exc:
            return StepResult(
                error=f"Slack rejected the bot token: {exc} (code={exc.code})",
                error_pointer="/bot_token",
            )

        team_id = str(auth.get("team_id", "") or "")
        user_id = str(auth.get("user_id", "") or "")

        writes = {"SLACK_BOT_TOKEN": bot_token}
        if team_id:
            writes["SLACK_TEAM_ID"] = team_id
        ctx.secrets.write(_per_bot(ctx.bot_name, writes))
        ctx.session[_K_OAUTH_DONE] = True

        return StepResult(
            next_step="paste_app_token",
            message=(
                f"Bot token validated (workspace: {auth.get('team', '?')}, "
                f"bot user: {user_id}). One more step."
            ),
        )

    # ------------------------------------------------------------------
    # LEGACY Step 1 — manifest registration via apps.manifest.create.
    # Kept for distributable-app workflows (still callable from
    # ``provision('config_token', …)``). NOT surfaced by wizard_steps()
    # anymore — that path produced orphan apps for non-distributable
    # workspaces (spec 08 D10, project memory project_slack_install_models).
    # ------------------------------------------------------------------

    def _step_config_token(
        self, inputs: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        config_token = str(inputs.get("config_token", "")).strip()
        if not config_token:
            return StepResult(
                error="config_token is required", error_pointer="/config_token"
            )

        refresh_token = str(inputs.get("refresh_token", "")).strip() or None
        display_name = (str(inputs.get("display_name", "")).strip()
                        or "hyperagent")
        include_private = _truthy(inputs.get("include_private_channels", True))
        include_dms = _truthy(inputs.get("include_dms", True))

        # Compose the redirect URL from the daemon's externally-
        # reachable base. The Flask handler passes ``host_base_url``;
        # the CLI path passes a configured value. Fall back to
        # localhost so unit tests and local-only flows work.
        base = ctx.host_base_url or "http://localhost:50080"
        redirect_url = (
            f"{base.rstrip('/')}/channels_oauth_callback"
            f"?channel_type={self.channel_type}"
            f"&session_id={ctx.session_id}"
        )

        # Stash refresh token for the rotate-and-retry path. Never
        # persisted on disk — disappears when the session evicts.
        if refresh_token:
            ctx.session[_K_REFRESH_TOKEN] = refresh_token

        manifest = build_slack_manifest(
            display_name=display_name,
            redirect_url=redirect_url,
            include_private_channels=include_private,
            include_dms=include_dms,
        )

        try:
            response = create_app_from_manifest(config_token, manifest)
        except SlackTokenExpiredError:
            # Try once with a rotated token.
            if not refresh_token:
                return StepResult(
                    error=(
                        "The config access token has expired. Provide a "
                        "refresh token to enable auto-rotation, or "
                        "generate a fresh access token at "
                        "https://api.slack.com/apps and try again."
                    )
                )
            try:
                rot = rotate_config_token(refresh_token)
                new_access = rot.get("token") or rot.get("access_token")
                new_refresh = rot.get("refresh_token") or refresh_token
                if not new_access:
                    raise SlackApiError(
                        "rotate response missing access token",
                        code="invalid_response",
                    )
                ctx.session[_K_REFRESH_TOKEN] = new_refresh
                response = create_app_from_manifest(new_access, manifest)
            except SlackApiError as exc:
                return StepResult(
                    error=f"token rotation failed: {exc} (code={exc.code})"
                )
        except SlackManifestError as exc:
            pointer = (
                exc.manifest_errors[0]["pointer"]
                if exc.manifest_errors
                else None
            )
            details = "; ".join(
                f"{e.get('pointer', '?')}: {e.get('message', '?')}"
                for e in exc.manifest_errors
            )
            return StepResult(
                error=f"Slack rejected the manifest: {details}",
                error_pointer=pointer,
            )
        except SlackApiError as exc:
            return StepResult(error=f"{exc} (code={exc.code})")

        # Parse out app credentials + install URL.
        app_id = str(response.get("app_id", "")).strip()
        creds = response.get("credentials") or {}
        client_id = str(creds.get("client_id", "")).strip()
        client_secret = str(creds.get("client_secret", "")).strip()
        signing_secret = str(creds.get("signing_secret", "")).strip()
        install_url = str(response.get("oauth_authorize_url", "")).strip()

        if not (app_id and client_id and client_secret and signing_secret and install_url):
            return StepResult(
                error=(
                    "Slack response missing required fields "
                    "(app_id/credentials/oauth_authorize_url). "
                    "Re-check the response payload."
                ),
                extra={"response": response},
            )

        # Persist non-token credentials immediately. The bot/app
        # tokens come later (steps 2/3). All keys are passed through
        # ``secret_key_for_bot`` so a named bot writes the suffixed
        # form (``SLACK_APP_ID_HAZBOT`` etc.); legacy / default bots
        # stay on the bare keys their predecessors already used.
        ctx.secrets.write(_per_bot(ctx.bot_name, {
            "SLACK_APP_ID": app_id,
            "SLACK_CLIENT_ID": client_id,
            "SLACK_CLIENT_SECRET": client_secret,
            "SLACK_SIGNING_SECRET": signing_secret,
        }))

        # Session scratch — used by step 2 (OAuth callback).
        ctx.session[_K_APP_ID] = app_id
        ctx.session[_K_CLIENT_ID] = client_id
        ctx.session[_K_CLIENT_SECRET] = client_secret
        ctx.session[_K_REDIRECT_URL] = redirect_url
        ctx.session[_K_INSTALL_URL] = install_url

        # Mint a CSRF state token Slack will return in the redirect
        # query string; we embed it in the install URL. Also append
        # ``team=<id>`` (resolved via auth.test on the config token)
        # because apps created via ``apps.manifest.create`` are pinned
        # to a single workspace and Slack rejects the install URL with
        # ``invalid_team_for_non_distributed_app`` if the team param is
        # missing. We fetch the team id at install-URL-build time rather
        # than passing it through the wizard so the operator never has
        # to know their workspace id.
        state = ctx.new_state_token()
        team_id = ""
        try:
            team_payload = config_token_team_info(config_token)
            team_id = str(team_payload.get("team_id") or "")
        except SlackApiError as exc:
            # auth.test on a config token is well-supported, but if it
            # ever stops working we'd rather show the link without team=
            # and let the user retry than surface an opaque error.
            logger.warning(
                "config_token_team_info failed (%s); install URL will "
                "omit team= and may show invalid_team_for_non_distributed_app",
                exc.code,
            )
        sep = "&" if "?" in install_url else "?"
        full_install_url = f"{install_url}{sep}state={state}"
        if team_id:
            full_install_url = f"{full_install_url}&team={team_id}"

        return StepResult(
            next_step="install",
            message=(
                f"App {app_id} created. Click the install link to "
                f"authorize it."
            ),
            url_override=full_install_url,
            state_token=state,
            extra={"app_id": app_id, "install_url": full_install_url},
        )

    # ------------------------------------------------------------------
    # Step 2 — OAuth callback (browser-side path)
    # ------------------------------------------------------------------

    def oauth_callback(
        self, query: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        code = str(query.get("code", "")).strip()
        state = str(query.get("state", "")).strip()
        if not code:
            return StepResult(error="OAuth callback missing 'code' parameter")
        if not state or not ctx.consume_state_token(state):
            return StepResult(
                error=(
                    "OAuth state token invalid or expired. Re-start "
                    "the provisioning wizard."
                )
            )

        client_id = ctx.session.get(_K_CLIENT_ID)
        client_secret = ctx.session.get(_K_CLIENT_SECRET)
        redirect_url = ctx.session.get(_K_REDIRECT_URL)
        if not (client_id and client_secret and redirect_url):
            return StepResult(
                error=(
                    "Session is missing credentials from step 1. "
                    "Re-start the provisioning wizard."
                )
            )

        try:
            response = exchange_oauth_code(
                client_id=client_id,
                client_secret=client_secret,
                code=code,
                redirect_uri=redirect_url,
            )
        except SlackApiError as exc:
            return StepResult(error=f"OAuth code exchange failed: {exc}")

        bot_token = str(response.get("access_token", "")).strip()
        team = response.get("team") or {}
        team_id = str(team.get("id", "")).strip()

        if not bot_token:
            return StepResult(
                error="oauth.v2.access response missing access_token"
            )

        # Persist immediately so a daemon crash mid-flow doesn't lose
        # the bot token (the most expensive thing to re-acquire).
        ctx.secrets.write(_per_bot(ctx.bot_name, {
            "SLACK_BOT_TOKEN": bot_token,
            "SLACK_TEAM_ID": team_id,
        }))
        ctx.session[_K_OAUTH_DONE] = True

        # Best-effort xapp- mint via config-access token. We don't
        # have the config token any more (it was discarded after
        # step 1), so this path is only reachable when the user gave
        # us a refresh token and we still have a viable token in
        # scratch. Today we just skip and route to the paste step;
        # the docs are unstable enough that the paste-fallback is
        # the only reliable path.
        return StepResult(
            next_step="app_token",
            message=(
                "Bot token captured. One more step: generate the "
                "Socket-Mode app-level token."
            ),
        )

    # ------------------------------------------------------------------
    # Step 2-fallback — paste-back bot token
    # ------------------------------------------------------------------

    def _step_paste_bot_token(
        self, inputs: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        bot_token = str(inputs.get("bot_token", "")).strip()
        if not bot_token:
            return StepResult(
                error="bot_token is required", error_pointer="/bot_token"
            )
        if not bot_token.startswith("xoxb-"):
            return StepResult(
                error="Bot token must start with xoxb-",
                error_pointer="/bot_token",
            )

        ctx.secrets.write(_per_bot(ctx.bot_name, {"SLACK_BOT_TOKEN": bot_token}))
        ctx.session[_K_OAUTH_DONE] = True

        return StepResult(
            next_step="app_token",
            message="Bot token saved. One more step.",
        )

    # ------------------------------------------------------------------
    # Step 3 — app-level token
    # ------------------------------------------------------------------

    def _step_app_token(
        self, inputs: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        app_token = str(inputs.get("app_token", "")).strip()
        if not app_token:
            return StepResult(
                error="app_token is required", error_pointer="/app_token"
            )
        if not app_token.startswith("xapp-"):
            return StepResult(
                error="App-level token must start with xapp-",
                error_pointer="/app_token",
            )

        ctx.secrets.write(_per_bot(ctx.bot_name, {"SLACK_APP_TOKEN": app_token}))
        ctx.session[_K_APP_TOKEN_DONE] = True

        # All tokens present — append (or replace) the block under
        # this bot's name in channels.json. Spec 09 D5: list-shape
        # per platform. Falls back to single-bot dict-shape only when
        # ctx.bot_name is empty (CLI legacy path).
        block = self.channels_json_block(ctx)
        if ctx.bot_name:
            ctx.channels_config.set_bot_block(
                self.channel_type, ctx.bot_name, block
            )
        else:
            ctx.channels_config.update_block(self.channel_type, block)

        return StepResult(
            next_step="summary",
            message="App-level token saved. Ready to apply.",
        )

    # ------------------------------------------------------------------
    # Step 4 — terminal summary
    # ------------------------------------------------------------------

    def _step_summary(self, ctx: ProvisionContext) -> StepResult:
        # The block was written at step 3. Step 4 is purely UI.
        return StepResult(
            terminal=True,
            message=(
                "Slack channel configured. Click Apply to start the "
                "adapter."
            ),
            extra={
                "app_id": ctx.session.get(_K_APP_ID),
            },
        )

    # ------------------------------------------------------------------
    # Smoke test
    # ------------------------------------------------------------------

    def test_connection(self, ctx: ProvisionContext) -> str:
        from hyperagent0.channels.config import secret_key_for_bot

        key = secret_key_for_bot(ctx.bot_name, "SLACK_BOT_TOKEN")
        bot_token = ctx.secrets.read(key)
        if not bot_token:
            raise RuntimeError(
                f"{key} not configured; provision the channel first"
            )
        auth = auth_test(bot_token)
        user = auth.get("user", "?")
        team = auth.get("team", "?")
        return f"Connected to Slack as @{user} in workspace {team}."

    def smoke_post(self, ctx: ProvisionContext, *, channel: str, text: str) -> None:
        """Optional helper for UI 'send a test message' button."""
        from hyperagent0.channels.config import secret_key_for_bot

        key = secret_key_for_bot(ctx.bot_name, "SLACK_BOT_TOKEN")
        bot_token = ctx.secrets.read(key)
        if not bot_token:
            raise RuntimeError(f"{key} not configured")
        chat_post_message(bot_token, channel, text)

    # ------------------------------------------------------------------
    # channels.json block (D6 — placeholders only, never raw secrets)
    # ------------------------------------------------------------------

    def channels_json_block(self, ctx: ProvisionContext) -> dict[str, Any]:
        # Preserve any non-token fields a prior install set (e.g.
        # allowed_users / require_mention configured manually). When
        # this bot already exists in list-shape channels.json, prefer
        # its own block over the platform's first entry.
        from hyperagent0.channels.config import secret_key_for_bot

        if ctx.bot_name:
            existing = ctx.channels_config.read_bot_block(
                self.channel_type, ctx.bot_name
            )
        else:
            existing = ctx.channels_config.read_block(self.channel_type)

        bot_token_key = secret_key_for_bot(ctx.bot_name, "SLACK_BOT_TOKEN")
        app_token_key = secret_key_for_bot(ctx.bot_name, "SLACK_APP_TOKEN")

        block = dict(existing)
        block.update(
            {
                "enabled": True,
                "token": f"$$secret({bot_token_key})",
                "app_token": f"$$secret({app_token_key})",
            }
        )
        # Set sensible defaults only if absent — don't clobber user-
        # set values.
        block.setdefault("require_mention", False)
        block.setdefault("project_binding", {})
        block.setdefault("allowed_users", [])
        block.setdefault("allowed_chats", [])
        return block


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _per_bot(bot_name: str, values: dict[str, str]) -> dict[str, str]:
    """Rewrite a bare-key secrets dict into per-bot suffixed keys.

    Spec 09 task 1.14: secrets land under ``KEY_<BOTNAME>`` so each
    bot's tokens are independent. Legacy / empty bot names round-trip
    unchanged via :func:`secret_key_for_bot`.
    """

    from hyperagent0.channels.config import secret_key_for_bot

    return {secret_key_for_bot(bot_name, k): v for k, v in values.items()}


def _truthy(value: Any) -> bool:
    """Accept the various "true"-shaped inputs the UI / CLI may send."""

    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


# Register at import time — mirrors the runtime adapter pattern.
register_provisioner(SlackProvisioner.channel_type, SlackProvisioner)
