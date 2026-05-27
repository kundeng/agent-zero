---
spec_id: 08-channel-provisioning-ux
status: SHIPPED
since: 2026-05-22
until: null
epic: channels
features: [channel-provisioner-protocol, channels-settings-tab, generic-wizard-renderer, slack-provisioner, telegram-provisioner, discord-provisioner, oauth-callback-router, channel-project-binding-ui, channel-cli-shim]
supersedes: []
superseded_by: null
depends_on: [04-chat-channels, 06-channel-hardening]
---

# Channel Provisioning UX (generic framework + per-platform impls)

<!-- The YAML above is the single source of truth for status and
     relationships. Never edit it outside /spec-plan. -->

## Context

Spec 04 shipped the channel **runtime** — `BaseChannel` ABC + registry + Telegram/Slack/Discord adapters, 42 tests, full Socket Mode. Spec 06 hardened it (`is_mention`, `reply_to`, retries, migrations). **Both cover the daemon side.** Neither covers how a user goes from "fresh install" to "live bot" — today that requires editing `~/.hyperagent0/channels.json` by hand, knowing the `$$secret()` placeholder syntax, manually creating apps at each platform's developer portal, and pasting four-to-six different tokens.

Spec 08 builds the **operator UX**: a generic provisioning framework that mirrors spec 04's adapter pattern, with Slack as the proving implementation and Telegram/Discord landing on the same shape.

### The general architecture

Spec 04 established the pattern at the runtime layer:

```
BaseChannel ABC (base.py)
├── register_channel(name, cls)  -> _REGISTRY dict
├── SlackChannel(BaseChannel)    -> registers itself
├── TelegramChannel(BaseChannel) -> registers itself
└── DiscordChannel(BaseChannel)  -> registers itself
```

Spec 08 mirrors that pattern at the **provisioning** layer:

```
BaseProvisioner ABC (provision/base.py)
├── register_provisioner(name, cls) -> _PROVISIONER_REGISTRY dict
├── SlackProvisioner(BaseProvisioner)    -> declares wizard steps + impl
├── TelegramProvisioner(BaseProvisioner) -> declares wizard steps + impl
└── DiscordProvisioner(BaseProvisioner)  -> declares wizard steps + impl
```

Plus a thin generic surface around them:

- **One** Flask handler `python/api/channels_provision.py` that dispatches on `channel_type`
- **One** OAuth-callback handler `python/api/channels_oauth_callback.py` that dispatches on path-prefix
- **One** Alpine wizard component (`webui/components/settings/channels/wizard.html`) that renders steps from a JSON description returned by the provisioner
- **One** CLI subcommand `haz channel provision <platform> ...` that introspects the provisioner's declared steps

The result: adding a fourth platform (Mattermost, Matrix, WhatsApp Business, …) is a single new class implementing the protocol plus a `register_provisioner()` call — same shape as adding a new `BaseChannel` adapter. **No new framework code, no new Flask routes, no new UI files.**

### Slack-specific notes (the proving implementation)

Slack's **manifest API** (`apps.manifest.create`) is what makes this UX possible at all. The user generates one workspace-level **configuration access token** at https://api.slack.com/apps (configuration tokens section), pastes it into the wizard, and one POST registers the app, defines all scopes, enables Socket Mode, and returns the install URL. Two human steps remain: clicking "Install to workspace" once (auto-captured via OAuth callback when the browser can reach haz; pasted otherwise), and obtaining the Socket Mode app-level token (`xapp-…`, attempted via API then paste fallback).

Telegram and Discord don't have manifest APIs of the same caliber, but their provisioning is simpler — paste a bot token from BotFather / Discord Developer Portal, optionally drive a couple of secondary API calls (`setMyCommands` for Telegram, invite URL generation for Discord). All three fit the same `BaseProvisioner` shape.

### What this spec does NOT cover

- Adding the actual NanoClaw Chat SDK bridge for 20+ extra platforms (spec 04.3.2 still open).
- Rewriting the Slack Socket Mode runtime — that's `hyperagent0/channels/slack.py` and stays as-is.
- Refresh-on-expiry of the Slack *config-access token* in long-running daemons — config tokens are one-shot at provision time.
- Project provisioning UI. Channel ↔ project binding lives here (it's how channels reach projects), but creating/editing projects themselves is out of scope.

## Constraints

- **Zero patches to existing `python/*.py` files.** New files under `python/api/` are allowed (that directory is upstream's drop-in convention for API handlers — the auto-loader at `run_ui.py:501` scans it). All real logic lives under `hyperagent0/channels/provision/`.
- **Tokens never appear in plaintext** in `~/.hyperagent0/channels.json`. Use `$$secret(NAME)` placeholders; cleartext lives only in `usr/secrets.env`, written via the existing `SecretsManager.save_secrets_with_merge`.
- **Bootstrap-only credentials never persist.** Slack's config-access token is used once in-memory, then discarded. The optional refresh-token may persist only if the user explicitly opts in (P3).
- **Cold-start budget (spec 03 D5)**: `haz --help` and `haz status` stay <200ms. Provisioner modules import lazily — only when the Flask handler or CLI subcommand runs.
- **HTTP client**: stdlib `urllib.request` only. No new entries in `requirements.txt`. `slack_sdk` / `slack_bolt` / `python-telegram-bot` / `discord.py` stay as `[<platform>]` extras for the runtime adapter; provisioning does **not** require them (provisioning is HTTP-only).
- **Non-blocking**: provisioner methods may make slow HTTP calls. Flask handlers wrap them in a thread executor so the agent event loop is not blocked.

## Decisions

### D1: `BaseProvisioner` ABC + registry (mirrors `BaseChannel`)

**Choice**: New module `hyperagent0/channels/provision/base.py`. Every platform implements:

```python
class BaseProvisioner(ABC):
    channel_type: str                              # "slack" / "telegram" / "discord"
    required_secrets: list[str]                    # secret keys this provisioner writes
    bootstrap_url: Optional[str] = None            # where to get the entry credential (e.g. api.slack.com/apps)

    @abstractmethod
    def wizard_steps(self) -> list[WizardStep]:    # declarative; UI + CLI both read this
        ...

    @abstractmethod
    def provision(self, step_id: str, inputs: dict[str, Any], ctx: ProvisionContext) -> StepResult:
        """Execute one step. Returns next-step instruction or terminal result."""

    @abstractmethod
    def oauth_callback(self, query: dict, ctx: ProvisionContext) -> StepResult:
        """Optional — only platforms with browser-OAuth implement this non-trivially."""

    @abstractmethod
    def test_connection(self, ctx: ProvisionContext) -> str:
        """Smoke test (post 'hello' or call a no-op endpoint). Return user-facing message."""

    @abstractmethod
    def channels_json_block(self, ctx: ProvisionContext) -> dict:
        """Build the dict that lands in channels.json[<channel_type>] — uses $$secret() placeholders only."""
```

`register_provisioner(name, cls)` + `_PROVISIONER_REGISTRY` mirror the spec-04 channel registry.

**Why**: Same shape as `BaseChannel`. Future platforms drop in as one new class file plus a registration line. Framework code stays platform-agnostic.

### D2: Declarative `WizardStep` shape — UI and CLI both consume it

**Choice**: Provisioners describe their flow as a list of step descriptors. Step kinds:

| Kind | UI rendering | CLI rendering |
|------|--------------|--------------|
| `input` | Form with one or more fields (text/password/select) | `--<field-id>` flag per field |
| `link_with_callback` | "Click to authorize" → opens popup → waits postMessage with timeout | Prints URL, then polls a local endpoint or waits for `--code` flag |
| `link_with_paste` | "Click to open, then paste result below" | Prints URL + reads from stdin / `--<field-id>` flag |
| `info` | Read-only message ("Slack will email you …") | Echoed |
| `summary` | Recap with "Apply" button | Confirmation prompt |

Each step carries `id`, `label`, `help_text`, `fields[]` (for input kinds), `next_on_success`, `next_on_timeout` (for callback kinds).

**Why**: Decouples the UI from the provisioner code. The Slack wizard happens to have four steps; Telegram's is one. The Alpine renderer doesn't care. The CLI introspects the same descriptor to map flags. A reviewer reading `SlackProvisioner.wizard_steps()` can see the entire flow in one place.

### D3: Generic Flask handlers — one per verb, dispatch on `channel_type`

**Choice**: Instead of `channels_provision_slack.py`, `channels_provision_telegram.py`, etc., we have **one** handler per verb that dispatches via the registry:

| Route | Method | Body / query | Action |
|-------|--------|--------------|--------|
| `/channels_status` | GET | — | List configured channels + live-adapter dots from `lifecycle._running_adapters` |
| `/channels_wizard/<channel_type>` | GET | — | Return the provisioner's `wizard_steps()` as JSON |
| `/channels_provision` | POST | `{channel_type, step_id, inputs}` | Call `provisioner.provision(step_id, inputs, ctx)`; return `StepResult` |
| `/oauth/<channel_type>/callback` | GET | platform query string | Call `provisioner.oauth_callback(query, ctx)`; return HTML that posts back to `window.opener` |
| `/channels_test/<channel_type>` | POST | — | Call `provisioner.test_connection(ctx)` |
| `/channels_apply` | POST | — | Call `lifecycle.restart_channels()` |
| `/channels_bind_project` | POST | `{channel, chat_id?, project}` | Update `project_binding` in channels.json (no restart) |

**Why**: One file per verb stays trivial. The platform-specific logic stays in the provisioner classes where reviewers expect to find it. A new platform = zero new Flask files.

### D4: OAuth callback URL is `/channels_oauth_callback?channel_type=<name>`

**Choice**: Each platform that uses browser OAuth declares the URL pattern in its manifest / app config as `http(s)://<haz-host>:<port>/channels_oauth_callback?channel_type=<name>`. The single handler dispatches via the ``channel_type`` query parameter.

The path-segment shape (``/oauth/<channel_type>/callback``) would have been cleaner but would require modifying Agent Zero's upstream API auto-loader at ``run_ui.py:494``, which derives the route from the handler's module name (``f"/{name}"``). Patching ``run_ui.py`` violates the conflict-surface budget. Query-string dispatch is behaviorally identical because OAuth 2.0 (RFC 6749 §4.1.2) requires the redirect URI to match exactly across the authorize → callback round-trip, and platforms preserve query parameters across the redirect.

CSRF: the standard ``state`` parameter — provisioners mint it via :meth:`ProvisionContext.new_state_token`, embed it in the install URL, and verify on callback via :meth:`ProvisionContext.consume_state_token`. The handler itself does NOT require Agent Zero's CSRF middleware (the redirect originates from Slack, which has no cookie) or auth (Slack has no session).

**Why**: Symmetric with D3 — one handler, registry-driven dispatch — without an upstream patch. Slack uses it; Telegram/Discord don't (bot-token paste). The pattern is reserved so platforms that need OAuth later (Matrix Synapse, Microsoft Teams, …) plug in without new routing.

### D5: `ProvisionContext` carries cross-step state

**Choice**:

```python
@dataclass
class ProvisionContext:
    channel_type: str
    session_id: str                    # so multi-step flows correlate
    session: dict[str, Any]            # provisioner-scoped scratch (state token, app_id from step 1, ...)
    secrets: SecretsBridge             # write-only API to usr/secrets.env
    channels_config: ChannelsConfigBridge  # read/write API to channels.json
```

The Flask handlers maintain a small in-memory `session_id → session_dict` cache (TTL 30 min). The session id is round-tripped by the UI as a header or query param.

**Why**: Provision flows are inherently multi-step and stateful (Slack's step 2 needs the `app_id` and `client_secret` from step 1). Letting the provisioner stash state in the context object beats threading it through every method signature. Cache is in-memory only — a daemon restart in the middle of a provision means the user starts over (acceptable; provisioning takes ~2 minutes end-to-end).

### D6: Secrets storage — provisioners declare their key names

**Choice**: Each provisioner exposes `required_secrets: list[str]` of *bare* key names: Slack declares `[SLACK_APP_ID, SLACK_SIGNING_SECRET, SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_TEAM_ID]`. Telegram declares `[TELEGRAM_BOT_TOKEN]`. Discord declares `[DISCORD_BOT_TOKEN]`.

The `SecretsBridge` in `ProvisionContext` writes only into the declared set — anything else is a programming error and raises. All writes go through `SecretsManager.save_secrets_with_merge` (single atomic write per provisioner step).

`channels.json`'s per-platform block carries only `$$secret(NAME)` placeholders for the token-shaped keys, plus non-secret fields (`enabled`, `require_mention`, `project_binding`, `allowed_users`, `allowed_chats`).

**Per-bot suffix convention (spec 09 P1.14, refines D6)**: When a provisioner runs with a non-empty / non-legacy `ctx.bot_name`, secrets land under `KEY_<BOTNAME>` (uppercase, dashes → underscores). E.g. bot `hazbot` writes `SLACK_BOT_TOKEN_HAZBOT`. The `LEGACY_BOT_NAMES = {"_legacy", "default", ""}` set keeps the bare-key form for back-compat — old single-bot installs never rewrite their existing `SLACK_BOT_TOKEN`. The helper is `hyperagent0.channels.config.secret_key_for_bot(bot_name, key)`; all three provisioners route their writes and `$$secret(...)` placeholder rendering through it.

**Why**: Matches the existing `ChannelConfig` placeholder convention. Operators have one consistent place to rotate keys. Per-platform `required_secrets` lists let `haz channel show` print "what tokens does this platform need?" without hardcoding. The per-bot suffix lets two bots on the same platform have independent secret stores without colliding.

### D7: Channel ↔ project binding gets a UI but no schema change

**Choice**: The existing `ChannelConfig.project_binding` dict (already implemented at `hyperagent0/channels/config.py:78-132`) gets a UI:

- "Default project for this channel" → `project_binding["default"]`
- "Per-chat overrides" → table of `(chat_id, project_name)` rows that write `project_binding[chat_id]`
- Saving the binding does NOT require a daemon restart. The router reads `channel_config.project_for_chat(...)` per inbound message, so updates are live.

**Why**: Spec 04 already shipped this; UI just exposes the existing schema. "On demand" mapping = the user can change the binding any time from the Channels tab without restarting anything. Same UI works for every platform because `project_binding` is on the generic `ChannelConfig`.

### D8: CLI is a thin wrapper, also driven by `wizard_steps()`

**Choice**: `haz channel provision <platform>` introspects the registered provisioner's `wizard_steps()` and:

- Maps each `input` field to a `--<field-id>` Click option.
- For `link_with_callback` / `link_with_paste`, prints the URL and either polls a local HTTP endpoint (if daemon is up and reachable) or reads from stdin.
- If the daemon is running on `localhost:<port>`, posts to `/channels_provision` over loopback so state lives in one place.
- If the daemon is NOT running, invokes the same provisioner methods in-process and writes secrets/config directly.

`haz channel list` shows registered provisioners. `haz channel status` mirrors `/channels_status`.

**Why**: The wizard description is the source of truth for "what inputs does this platform need?" — the CLI shouldn't re-encode that information. Headless installs (Docker on a server, CI) get the same provisioning power as the UI without separate code paths.

### D10: Slack install model — paste-manifest in UI is the primary path

**Added 2026-05-23 after live-test discovery.**

The original D3/D4 design assumed Slack's `/oauth/v2/authorize` flow would work for capturing the bot token after the user clicks Allow. Live testing against the BayesLearner workspace proved this is wrong for the common case:

1. Apps created via `apps.manifest.create` with a workspace config token are **non-distributable** by default. Their `oauth_authorize_url` returns `invalid_team_for_non_distributed_app` even with the correct `&team=<id>` appended. OAuth v2 simply does not accept non-distributable apps.
2. Worse: those API-created apps are **orphans** — they don't appear in any user's "Your Apps" list at https://api.slack.com/apps because they were not created by an interactive developer session. The user cannot install them through the Slack admin UI either, because they can't find them.
3. Slack's CLI has a `slack app install` command, but `slack login` returns "This workspace is not eligible for the next generation Slack platform" for any workspace not enrolled in Slack's developer program (most free + standard paid plans).
4. The Slack API has an `apps.install` endpoint, but it returns `not_allowed_token_type` for config tokens — it only accepts admin user tokens (which the CLI mints, but the CLI is locked).

**Choice**: For personal/standard workspaces (the common case), the wizard generates the manifest JSON but does **not** call `apps.manifest.create`. Instead, it instructs the user to:

1. Go to https://api.slack.com/apps → click "Create New App" → "From a manifest" → paste the JSON we generate.
2. Land on the new app's page (now owned by their developer identity). Sidebar → "Install App" → "Install to Workspace" → Allow. The bot token is displayed; user copies it.
3. Paste the bot token back. Wizard writes it to secrets.
4. User generates the `xapp-` Socket Mode token via the same app page's "Basic Information → App-Level Tokens → Generate" UI (no API for this — Slack-enforced).
5. Paste xapp- back. Wizard writes it. Channel adapter restarts. Bot live.

For **distributable** apps (Slack App Directory listing or enterprise-internal distribution), the original OAuth callback flow still works. The wizard should detect `is_distributable` in the manifest and only show the OAuth auto-capture step on that path.

**Why**: Honest about Slack's design. The bot-install step is a Slack-enforced security boundary; no token, CLI, or API call gets around it for non-distributable apps. The paste-manifest flow is what every Slack chatbot project I checked actually uses — including NanoClaw. The original "3 clicks, fully automated" promise in D3 was based on a wrong assumption about OAuth v2 scope. This decision documents the correction.

**Implementation status (2026-05-23)**: code currently still does the apps.manifest.create call (D3 path), which creates orphan apps. **Next session must rework the Slack wizard** to skip the create step and just hand the user the manifest JSON to paste in their UI. The OAuth callback handler can stay for distributable apps but should be gated.

**Implementation done (2026-05-25)**: Shipped. `wizard_steps()` now
returns the D10 four-step flow:

1. `manifest_config` — collect `bot_name`, `display_name`, privacy
   options. No Slack API call. Generates the manifest JSON and stashes
   it in session state + `StepResult.extra["manifest_json"]` and
   embeds it in `StepResult.message` so even minimal renderers (CLI)
   surface something copyable.
2. `paste_bot_token` (kind=`link_with_paste`) — URL to
   api.slack.com/apps, paste field for the xoxb- token. Calls
   `auth.test` upfront so a typo'd token doesn't silently fail at
   adapter-start time.
3. `paste_app_token` — paste the xapp- Socket Mode token.
4. `summary` — terminal.

The legacy D3 path (`config_token` step → `apps.manifest.create` →
OAuth callback → `install_paste_fallback`) is still callable from
`provision()` for distributable-app workflows but is not surfaced by
`wizard_steps()`. Tests cover both paths (29 in
`tests/test_channels_provision_slack.py`, +5 D10-specific).

### D9: Adapter hardening while we're here

**Choice**: Close two small known gaps in `hyperagent0/channels/slack.py` flagged during the original Slack live-test session (the `NEXT_SESSION_SLACK.md` brief that drove this spec — since deleted, contents absorbed here and in the Log):

1. **Event-ID dedup**: bounded LRU set (`collections.OrderedDict`, cap 1024) on the adapter; drop on duplicate `event_id` from Slack's payload envelope.
2. **Own-bot filter**: ignore messages where `event["bot_id"]` equals our own bot id (from `auth.test`). Today the filter only checks `subtype`, so a self-routed message could loop.

**Why**: Both bugs surface the moment a real Slack workspace stresses the bot (network retries cause Slack to redeliver events; the bot's own thread replies show up as inbound messages). Cheap to fix in the same spec since we're already in `slack.py`.

## Tasks

<!-- [ ] pending | [x] done | [~] skipped | [!] BLOCKED: reason -->

### P1 — Must Do (framework + Slack as proving implementation)

#### Framework
- [ ] 1.1 `hyperagent0/channels/provision/__init__.py` — re-exports `BaseProvisioner`, `register_provisioner`, `get_provisioner`, `registered_provisioners`. No SDK imports.
- [ ] 1.2 `hyperagent0/channels/provision/base.py`:
  - `BaseProvisioner` ABC with abstract methods (D1)
  - `WizardStep`, `WizardField`, `StepResult` dataclasses (D2)
  - `ProvisionContext`, `SecretsBridge`, `ChannelsConfigBridge` (D5)
  - `_PROVISIONER_REGISTRY` + `register_provisioner` + `get_provisioner` + `registered_provisioners`
  - `step_result_to_json(...)` helper for HTTP serialization
- [ ] 1.3 `hyperagent0/channels/provision/sessions.py` — in-memory session cache `{session_id: {channel_type, scratch}}` with 30-min TTL, used by `ProvisionContext`
- [ ] 1.4 `hyperagent0/channels/secrets_bridge.py` — implementation of `SecretsBridge`; calls `SecretsManager.save_secrets_with_merge` lazily-imported
- [ ] 1.5 `hyperagent0/channels/channels_config_bridge.py` — implementation of `ChannelsConfigBridge`; reads/writes `~/.hyperagent0/channels.json` with atomic-rename
- [ ] 1.6 `hyperagent0/channels/lifecycle.py` — add `restart_channels()` helper (stop_all + start_enabled under the existing lock)

#### Generic Flask handlers
- [ ] 1.7 `python/api/channels_status.py` — GET, returns `{[channel_type]: {enabled, live, last_error, required_secrets, configured_secrets}}` reading channels.json + `lifecycle._running_adapters`
- [ ] 1.8 `python/api/channels_wizard.py` — GET `/channels_wizard/<channel_type>`, returns `provisioner.wizard_steps()` serialized
- [ ] 1.9 `python/api/channels_provision.py` — POST, body `{channel_type, step_id, inputs, session_id?}` → dispatch to provisioner, return `StepResult`
- [ ] 1.10 `python/api/channels_oauth_callback.py` — GET, path `/oauth/<channel_type>/callback`, dispatch to `provisioner.oauth_callback(query, ctx)`, return HTML that `window.opener.postMessage`s the result
- [ ] 1.11 `python/api/channels_apply.py` — POST, calls `lifecycle.restart_channels()`. Idempotent.
- [ ] 1.12 `python/api/channels_bind_project.py` — POST `{channel, chat_id?, project}`, writes `project_binding` in channels.json; no restart
- [ ] 1.13 `python/api/channels_test.py` — POST `/channels_test/<channel_type>`, calls `provisioner.test_connection(ctx)`

#### Slack provisioner (first implementation)
- [ ] 1.14 `hyperagent0/channels/provision/slack.py`:
  - `SlackProvisioner(BaseProvisioner)` registered as `"slack"`
  - `wizard_steps()` returns 4 steps: config-token input → install link-with-callback (90s) → xapp- input (link-with-paste, with API-first attempt) → summary
  - `provision()` cases: step `"config_token"` calls `apps.manifest.create`, writes signing_secret + app_id + client_id/secret to secrets, returns step 2 with the install URL + redirect_url
  - `oauth_callback()` exchanges code → writes `SLACK_BOT_TOKEN` + `SLACK_TEAM_ID`, tries `try_mint_app_token()`, returns step 3 (which auto-skips if mint succeeded)
  - `provision()` step `"xapp_paste"` writes `SLACK_APP_TOKEN`, flips `enabled=true`, returns terminal
  - `test_connection()` posts a "hyperagent0 channel test" message to `#general` (or a configured test channel)
  - `channels_json_block()` returns `{enabled: bool, token: "$$secret(SLACK_BOT_TOKEN)", app_token: "$$secret(SLACK_APP_TOKEN)", project_binding: {}, allowed_users: [], allowed_chats: [], require_mention: false}`
- [ ] 1.15 `hyperagent0/channels/provision/slack_manifest.py` — `build_slack_manifest(display_name, redirect_url, **opts) -> dict` (D5)
- [ ] 1.16 `hyperagent0/channels/provision/slack_api.py` — pure HTTP wrappers used by `SlackProvisioner` (`apps.manifest.create`, `oauth.v2.access`, `tooling.tokens.rotate`, `try_mint_app_token`, `chat.postMessage`); urllib only

#### Adapter hardening (D9)
- [ ] 1.17 `hyperagent0/channels/slack.py` — event_id LRU dedup + own-bot bot_id filter

#### Web UI — generic
- [ ] 1.18 `webui/components/settings/channels/channels-store.js` — Alpine store: fetches `/channels_status` on open, polls every 15s while modal open, owns the active-wizard state machine
- [ ] 1.19 `webui/components/settings/channels/channels.html` — Settings tab content: list of registered channels (driven by `/channels_status`), each card has status dot + enable toggle + "Provision" / "Re-provision" + "Bind to project"
- [ ] 1.20 `webui/components/settings/channels/wizard.html` — generic wizard renderer; consumes the step descriptor JSON and renders the four step kinds (D2); handles the popup + postMessage flow for `link_with_callback`
- [~] 1.21 ~~`bind-project.html`~~ — consolidated into `channels.html` as an inline input on each card (D7). A separate modal added no value for the "default project for this channel" case, which is what the spec actually requires; per-chat-id overrides will live in P3 if the need surfaces.
- [ ] 1.22 Wire the "Channels" tab into `webui/components/settings/settings.html` (single tab-block addition matching the existing pattern)

#### CLI shim
- [ ] 1.23 `hyperagent0/cli_commands/channel.py` (D8):
  - `haz channel list` — registered provisioners + `bootstrap_url`s
  - `haz channel status` — mirrors `/channels_status`
  - `haz channel provision <platform> [flags introspected from wizard_steps]`
  - `haz channel apply`
  - Register under `_SUBCOMMANDS` in `hyperagent0/cli.py`

### P2 — Should Do (Telegram + Discord provisioners + tests + docs)

#### Telegram provisioner
- [ ] 2.1 `hyperagent0/channels/provision/telegram.py`:
  - `TelegramProvisioner(BaseProvisioner)` registered as `"telegram"`
  - `bootstrap_url = "https://t.me/BotFather"`
  - `wizard_steps()`: 1 step (input: `bot_token`, optional `allowed_users` CSV)
  - `provision()` calls `getMe` to validate, writes `TELEGRAM_BOT_TOKEN`, optionally calls `setMyCommands`, flips `enabled=true`
  - `test_connection()` calls `getMe`, returns the bot username

#### Discord provisioner
- [ ] 2.2 `hyperagent0/channels/provision/discord.py`:
  - `DiscordProvisioner(BaseProvisioner)` registered as `"discord"`
  - `bootstrap_url = "https://discord.com/developers/applications"`
  - `wizard_steps()`: 2 steps (input: `bot_token` + `application_id`; info: invite URL with required intents + permissions)
  - `provision()` validates via `/users/@me`, writes `DISCORD_BOT_TOKEN`, generates the invite URL, flips `enabled=true`
  - `test_connection()` calls `/users/@me`, returns the bot username

#### Tests — framework
- [ ] 2.3 `tests/test_channels_provisioner_registry.py`:
  - `register_provisioner` + `get_provisioner` + `registered_provisioners` round-trip
  - Duplicate registration semantics
  - `WizardStep`/`StepResult` JSON round-trip
- [ ] 2.4 `tests/test_channels_provision_context.py`:
  - Session cache TTL + eviction
  - `SecretsBridge` rejects writes to undeclared keys
  - `ChannelsConfigBridge` atomic write + read-back

#### Tests — provisioners
- [ ] 2.5 `tests/test_channels_provision_slack.py`:
  - All `slack_api` HTTP calls mocked via monkeypatched `urllib.request.urlopen`
  - Happy path: 4 steps walked, `usr/secrets.env` ends with all 7 keys, `channels.json` carries placeholders
  - `invalid_manifest` response surfaces `errors[].pointer/message`
  - Expired config-token triggers `rotate_config_token`, retry succeeds
  - OAuth callback round-trip: code → `xoxb-` written, terminal step returned if xapp- mint succeeded
  - `try_mint_app_token` returns `None` → next step is paste, not auto-complete
- [ ] 2.6 `tests/test_channels_provision_telegram.py`:
  - Happy path + invalid bot_token (`getMe` returns 401)
- [ ] 2.7 `tests/test_channels_provision_discord.py`:
  - Happy path + invite URL generation

#### Tests — Flask handlers + adapter hardening
- [ ] 2.8 `tests/test_channels_api_handlers.py`:
  - Round-trip `/channels_status`, `/channels_wizard/<platform>`, `/channels_provision`, `/oauth/<platform>/callback`, `/channels_test/<platform>`, `/channels_apply`, `/channels_bind_project`
  - Stub provisioner classes registered for isolation
- [ ] 2.9 `tests/test_hyperagent0_channels_slack_hardening.py`:
  - `test_slack_dedups_repeated_event_id`
  - `test_slack_drops_own_bot_messages`
- [ ] 2.10 `tests/test_haz_channel_provision.py`:
  - CliRunner tests for the CLI shim
  - Daemon-not-running path (in-process) + daemon-running path (mock loopback POST)
  - Flag-introspection from `wizard_steps()` works for Slack, Telegram, Discord

#### Docs + steering
- [ ] 2.11 README — short "Wire a chat channel in 3 clicks" section pointing at the Channels tab; one example each for Slack / Telegram / Discord
- [ ] 2.12 `docs/steering/pillars.md` — bump the **MVP/Ship** entry to note channel-provisioning UX is the user-facing milestone for the channels epic; bump **Test** to note framework + 3 provisioners covered

### P2.5 — Live-test corrections (next session must do)

- [x] 2.5.1 Slack wizard: D10 path shipped 2026-05-25. The default wizard flow is now paste-manifest (`manifest_config` → `paste_bot_token` → `paste_app_token` → `summary`) — no `apps.manifest.create` call, no orphans. The legacy D3 path remains callable from `provision()` for distributable apps but is no longer the default. 5 new tests in `tests/test_channels_provision_slack.py`.
- [x] 2.5.2 Daemon's slack-bolt `invalid_auth` issue. **Resolved 2026-05-25**: no longer reproduces. Verified via full `haz start --port 50080` boot — uvicorn comes up, Bolt app prints "⚡️ Bolt app is running!", Socket Mode session establishes cleanly, no auth error. Most likely incidentally fixed by spec 09's `lifecycle.start_enabled_channels` rewrite (the multi-bot refactor restructured the order in which adapters are instantiated + connected). Diagnostic note: when reproducing daemon Slack issues, re-enable logging AFTER importing `run_ui` — that module sets root logger to WARNING at line 37, silencing INFO logs that hide what's actually happening.
- [ ] 2.5.3 Provide `haz slack run` standalone-mode command that boots only the channels stack (no UI, no LLM) for cases where the user wants a chat bot without the rest of agent-zero. Equivalent to `/tmp/slack-standalone.py` from the live test, but production-quality.

### P3 — Nice to Have

- [ ] 3.1 Persist Slack refresh-token (opt-in) so users can rebuild the app from the UI later without going back to api.slack.com
- [ ] 3.2 Slack message-events mirrored to the open web UI for monitoring without joining the channel
- [ ] 3.3 NanoClaw Chat SDK bridge as a single "external" provisioner that exposes the 20+ supported platforms through one `BaseProvisioner` impl (revisits spec 04.3.2)
- [ ] 3.4 Matrix / WhatsApp Business / Mattermost provisioners (drop-in additions once the framework proves out)
- [ ] 3.5 Webhook-mode option (in addition to Socket Mode) for users on hosts with stable public ingress

## Open Questions

- [ ] Slack's `oauth_config.redirect_urls` — list, single entry, or both supported in one manifest? Decide at implementation: probably single, with paste fallback as the alt path inside the same manifest variant.
- [ ] When the daemon is on a private host that Slack cannot reach for the OAuth redirect (Docker on a NAT'd LAN), the auto-callback fails and we drop to paste mode. Need a one-line UI string at the 90s timeout that explains this without scaring the user.
- [ ] `apps.connections.*` endpoint discovery — implementation-time check against current Slack docs. If no public endpoint mints xapp- tokens via a config-access token, the wizard's xapp- step is paste-only and we drop "trying API" from the copy.
- [ ] CSRF on the OAuth callback uses Slack's `state` parameter; need to confirm this fits the existing CSRF middleware shape (`requires_csrf()` on the handler base) and isn't double-checked.
- [ ] Should `provision()` step execution run in a thread executor (per-step) or in an asyncio executor (per-handler)? Either works; the latter keeps the agent event loop fully responsive. Decide at implementation against `run_ui.py`'s patterns.

## Log

**2026-05-22** — Drafted after course correction. The original next-session brief (then in `NEXT_SESSION_SLACK.md` at the repo root, deleted 2026-05-26 once its contents were absorbed) specified a `haz slack provision` CLI; the user pivoted to a web-UI extension. First draft of spec 08 baked Slack in. User flagged that the architecture must generalize across platforms, **not** treat Slack as a special case. Spec rewritten around `BaseProvisioner` + declarative `WizardStep` mirroring the spec-04 `BaseChannel` + registry pattern. Slack stays as the proving P1 implementation; Telegram + Discord land on the same framework in P2. Adding new platforms after this spec ships = one new class file plus a registration line, no framework/UI/CLI changes.

**2026-05-22 (build)** — Framework (1.1–1.6) and generic Flask layer (1.7–1.13) implemented. D4 refined during the build: the OAuth callback uses ``/channels_oauth_callback?channel_type=<name>`` instead of the originally-imagined ``/oauth/<channel_type>/callback`` path. Reason: the upstream API auto-loader at ``run_ui.py:494`` derives the route from the module name, so path-variable routing would require an upstream patch (which the spec's conflict-surface budget forbids). Query-string dispatch is behaviorally identical under OAuth 2.0 redirect-URI rules. Spec D4 updated.

**2026-05-22 (P1 complete)** — Slack provisioner (1.14–1.16) + adapter hardening (1.17) + Web UI (1.18–1.22) + CLI shim (1.23) all shipped on top of the framework. Committed as ``9bc9871``. End-to-end smoke verified: 4-step Slack flow walked with mocked HTTP writes all 7 secrets into ``usr/secrets.env``, populates ``channels.json`` with placeholder-only references, and never persists the config-access token. ``haz channel --help`` cold-start measured at ~17ms (well under the spec 03 D5 200ms budget).

**2026-05-22 (P2)** — Telegram (2.1) and Discord (2.2) provisioners added, each as a single class file with ``register_provisioner`` at the bottom. Eight test files (2.3–2.10) added: framework registry, context+bridges, Slack provisioner (20 tests), Telegram (11), Discord (12), Slack-adapter hardening (11), CLI shim (11). 101 new tests + 54 pre-existing = 155 passing. README updated with the "Wire up a chat channel" section (P2.6). ``docs/steering/pillars.md`` reflects the MVP/Ship + Test pillar advances (P2.7). Adding a fourth platform now needs one new class + one ``register_provisioner`` call — the framework claim holds.

**2026-05-23 (live-test against bayeslearner workspace)** —
- Drove the Slack wizard end-to-end against the user's real workspace using their config-access token (`xoxe.xoxp-…` minted at https://api.slack.com/apps).
- Created 4 apps via `apps.manifest.create` (A0B5PS8P2HG, A0B5MQPF0A2, A0B5HGVKKS7, A0B5Q702BA6). Each `oauth_authorize_url` failed with `invalid_team_for_non_distributed_app` — Slack does not run the OAuth v2 install flow for non-distributable apps. **Wizard's `install` step (kind=link_with_callback) is dead code for this app type.**
- The four apps were INVISIBLE in the user's "Your Apps" list at api.slack.com/apps — config-token-created apps are orphans not owned by any developer identity. No path through Slack admin UI to install them. All four deleted via `apps.manifest.delete`.
- Pivoted to "create app via UI from manifest" flow. User created app A0B5V01R4TE interactively from the JSON manifest we generate. That app DID appear in their UI with a working Install button. Captured `xoxb-` + `xapp-` via paste-back.
- **Bot is live** in bayeslearner workspace as @hazbot. Round-trips messages via the spec-04 SlackChannel adapter, running as a standalone Python process (`/tmp/slack-standalone.py`, mirrors lifecycle.py's invocation).
- **Bug found in live test**: spec-04 SlackChannel dispatched twice per @-mention because Slack fires both `message` and `app_mention` events for the same envelope. Fixed in `slack.py:_on_message` to skip when text contains bot mention token (then `_on_app_mention` handles it). Regression test added in `test_hyperagent0_channels_slack_hardening.py`.
- **Known issue (unresolved this session)**: when the same SlackChannel adapter runs inside the full daemon process (`haz start`), Socket Mode connection returns `invalid_auth` from `apps.connections.open`. Bisection ruled out individual upstream imports + the thread+loop pattern. The standalone runtime works perfectly. Root cause is somewhere in the full daemon's runtime (uvicorn + init_a0 + …) but I couldn't isolate it in this session. Daemon path is documented as known-broken; standalone runtime works as a temporary workaround.
- Added decision **D10** documenting the Slack install-model truth: paste-manifest-in-UI is primary, OAuth callback is for distributable apps only. **Next session must rework the wizard** to skip the apps.manifest.create call for non-distributable apps and just hand the user the JSON manifest with paste-back instructions. The current wizard creates orphans the user can't manage.
