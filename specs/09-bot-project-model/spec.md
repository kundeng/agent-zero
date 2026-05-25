---
spec_id: 09-bot-project-model
status: PARTIAL
since: 2026-05-24
until: null
epic: channels
features: [multi-bot-per-platform, default-project-entity, channel-config-list-schema, first-run-setup-wizard, threadstore-key-extension]
supersedes: []
superseded_by: null
depends_on: [04-chat-channels, 06-channel-hardening, 08-channel-provisioning-ux]
---

# Bot + Project Entity Model

<!-- The YAML above is the single source of truth for status and
     relationships. Never edit it outside /spec-plan. -->

## Context

Spec 04 + 08 shipped a working channel runtime + provisioning UX, but
both assume a single bot per platform per `haz` install. The schema
in `~/.hyperagent0/channels.json` is one block per platform:

```jsonc
{ "slack": {...one bot's tokens...}, "telegram": {...}, "discord": {...} }
```

User feedback (2026-05-24): two distinct bots in the same workspace,
each bound to a different project, is a real use case. Engineering
team's `@code-bot` runs against the engineering project; customer-ops'
`@support-bot` against a different project. Or one workspace runs both
a production bot and a dev bot. The current schema makes that
impossible without a second `haz` install.

The second realization in the same conversation: the existing
projectless / project-active branching in the codebase (4+ sites:
`system_prompt`, `code_exec.ensure_cwd`, `get_secrets_manager`,
`srt._ensure_profile`) is needless complexity. If a `_default` project
exists implicitly as a real entity with sensible defaults, every
branch collapses to one path. Project-bound chats just read the bound
project; "projectless" chats read `_default`.

## Constraints

- **Backward-compatible read path**: existing `channels.json` files in
  the wild (single-block-per-platform shape) must load without manual
  user migration. Migration runs on first daemon start.
- **Zero patches to existing `python/*.py`**: changes contained in
  `hyperagent0/`. New `_default` project lives at
  `usr/projects/_default/.a0proj/` with an empty `project.json`.
- **ThreadStore must migrate**: SQLite key is currently
  `(channel_type, chat_id)`. Becomes `(channel_type, bot_name, chat_id)`
  so two bots on the same platform don't collide on a shared chat id.
- **Cold-start budget unchanged**: lazy imports stay lazy.

## Decisions

### D1: `channels.json` is a list of bots per platform

**Choice**: New schema:

```jsonc
{
  "slack": [
    {
      "name": "hazbot",
      "token": "$$secret(SLACK_BOT_TOKEN_HAZBOT)",
      "app_token": "$$secret(SLACK_APP_TOKEN_HAZBOT)",
      "default_project": "engineering",
      "project_overrides": { "C012345": "customer-ops" },
      "allowed_users": [],
      "allowed_chats": [],
      "require_mention": false
    },
    {
      "name": "support-bot",
      "token": "$$secret(SLACK_BOT_TOKEN_SUPPORT)",
      "app_token": "$$secret(SLACK_APP_TOKEN_SUPPORT)",
      "default_project": "customer-ops"
    }
  ],
  "telegram": [...],
  "discord": [...]
}
```

`name` is the local identifier the operator uses (also surfaces in
logs and the Channels UI). It is NOT the bot's display name in Slack
— that's set in the manifest separately. Multiple bots on the same
platform are distinguished by `name` everywhere in code (logs,
ThreadStore keys, lifecycle adapters).

**Backward compat**: on load, if the value for a platform is a dict
(old schema) instead of a list, wrap it as `[{...with name='default'}]`
and write it back to disk. Single-shot migration, idempotent on
subsequent loads.

**Why**: the user's stated use case is real. Same workspace, different
bots, different projects. Doing this now (before the project capability
work in spec 10) prevents that work from baking the single-bot
assumption deeper.

### D2: `_default` is a real implicit project

**Choice**: At first daemon start, if `usr/projects/_default/.a0proj/`
does not exist, create it with:

```jsonc
// usr/projects/_default/.a0proj/project.json
{
  "title": "Default",
  "description": "Implicit project for chats with no explicit binding.",
  "color": "#888",
  "instructions": "",
  "git_url": ""
}
```

Empty `instructions/`, empty `knowledge/`, empty `skills/`,
`secrets.env` does not exist (so global secrets apply).

Every `_get_or_create_context` that today branches on
`if project_name:` now resolves `project_name = project_name or
"_default"` and always activates a project. The branches in
`system_prompt._10_system_prompt`, `code_execution_tool.ensure_cwd`,
`secrets.get_secrets_manager`, and `srt._ensure_profile` all collapse
to single-path code.

**Why**: simpler invariant. Less code. Better mental model — every
chat lives in *some* project. The implicit `_default` matches what
user already does (working in a global workdir with global secrets).

**Caveat**: the project folder for `_default` is
`usr/projects/_default`. If a user wants the sandbox/code_exec cwd to
be the legacy global `workdir_path`, they set `_default.project_folder
= workdir_path` in `project.json`. Migration writes the current
`workdir_path` from settings into `_default.project_folder` on first
create so behavior is preserved.

### D3: ThreadStore key is `(channel_type, bot_name, chat_id)`

**Choice**: Schema migration adds `bot_name TEXT NOT NULL DEFAULT '_legacy'`
column to `thread_map`. Composite unique key extended. Existing rows
get `bot_name = '_legacy'`; when those chats resume, the router maps
the legacy entries to whichever bot is named `'_legacy'` or the first
bot in the list as fallback.

**Why**: two bots on the same Slack workspace can both be DM'd by
the same user — `chat_id` alone doesn't disambiguate. The bot name
makes the key globally unique within the haz install.

### D4: First-run setup wizard nag

**Choice**: When the Web UI loads and `channels.json` is missing /
empty / contains only `_legacy` entries, the Channels tab shows a
banner: "No bots configured yet — set one up to chat from Slack /
Telegram / Discord." Click → opens the same wizard as Settings →
Channels.

Optional CLI hook: `haz status` prints a one-liner when no bots are
configured.

**Why**: lowers the friction of going from `haz start` → first
working bot. Currently a new user has to know that Settings → Channels
exists.

### D5: One adapter instance per bot in `lifecycle.py`

**Choice**: `start_enabled_channels` loops over each bot of each
platform, instantiates one `SlackChannel` (or `TelegramChannel`,
`DiscordChannel`) per bot, registers each with the router. The router
indexes adapters by `(channel_type, bot_name)`.

When sending a reply, the router needs to know which bot to use —
that's already implicit in the inbound's metadata (the adapter
that received the event). Outbound includes a `bot_name` field that
the router uses to look up the adapter.

**Why**: keeps adapter logic per-bot-stateless. Each bot has its own
Socket Mode connection, its own auth.test result, its own bot_id
self-filter. No cross-bot leakage.

## Tasks

### P1 — Must Do

- [x] 1.1 `hyperagent0/channels/config.py` — `ChannelConfig` becomes a per-bot dataclass. New `load_channels_config()` returns `dict[str, list[BotConfig]]`. Backward-compat loader detects dict-shape and wraps to single-element list.
- [x] 1.2 Migration: write back the normalized list-shape to `channels.json` on first load. Idempotent. `_maybe_persist_normalized()` writes atomically (tempfile + os.replace); skips when already in list-shape; opt-out via `persist_migration=False`.
- [x] 1.3 `hyperagent0/channels/migrations/002_bot_name.sql` — add `bot_name` column to `thread_map`, extend unique key. SQLite-safe rebuild via temp table swap inside BEGIN/COMMIT.
- [x] 1.4 `ThreadStore.get` / `.upsert` / `.touch` / `.all_rows` — all take `bot_name` kwarg (default `"_legacy"` matches migration column default).
- [x] 1.5 `ChannelRouter` — `self.channels` and `self.channel_configs` keyed by `(channel_type, bot_name)`. Dispatch uses `msg.bot_name`. `OutboundMessage` gains `bot_name` field. `_normalize_channel_key()` lets legacy callers pass plain `str` keys.
- [x] 1.6 `lifecycle.start_enabled_channels` — iterates `load_bot_configs()`, instantiates one adapter per enabled bot, registers each under `(channel_type, bot_name)`. `_channels` map keyed the same way. `running_adapters()` reports bare channel_type for `_legacy` (single-bot) installs so existing status UI keeps working.
- [x] 1.7 `BaseChannel` — `bot_name: str` attribute populated at construction. Slack/Telegram/Discord adapters propagate it and stamp it on the `InboundMessage`s they build.
- [x] 1.8 `_default` project bootstrap helper in `hyperagent0/projects.py`. Creates `usr/projects/_default/.a0proj/project.json` if missing. Called from lifecycle.start_enabled_channels at boot.
- [ ] 1.9 **DEFERRED 2026-05-24** — Collapse the projectless branches:
  - `python/extensions/system_prompt/_10_system_prompt.py:75` — always resolve project_name or "_default"
  - `python/tools/code_execution_tool.py:551` — same
  - `python/helpers/secrets.py:get_secrets_manager` — same
  - `hyperagent0/sandbox/srt.py:_ensure_profile` — same
  Bigger than a one-liner per site: each branch carries different alternate-path semantics (template swap, settings.workdir_path fallback, secrets file-append, sandbox write-allow path). True collapse requires upstream `get_project_folder` to honor a `project_folder` override in project.json + per-site behavior tests. Revisit in a follow-up session.
- [x] 1.10 First-run nag in `webui/components/settings/channels/channels.html` — show empty-state banner with "Get started" CTA.
- [x] 1.11 `haz status` hint when no bots configured.
- [x] 1.12 `channels-store.js` updated to handle list-of-bots shape per platform. Channels UI shows N bot cards per platform. `/channels_status` rewritten to emit one row per `(channel_type, bot_name)` plus a placeholder row per unconfigured platform; live lookup honors both bare `channel_type` (legacy) and `channel_type/bot_name` keys.
- [x] 1.13 Wizard updated: `bot_name` field added at the top of the first input step of every provisioner (Slack/Telegram/Discord). `dispatch.run_step` extracts it from inputs or session and threads it through `ctx.bot_name` and the per-bot `AllowlistedSecretsBridge`. Frontend `_suggestBotName()` picks the first available of `["default","bot1","bot2",...]`. Bridge gains `set_bot_block/read_bot_block/list_bot_names` for by-name upsert into the platform's list.
- [x] 1.14 Per-bot secret keys: `secret_key_for_bot(bot_name, key)` returns the bare key for `_legacy`/`default`/empty, otherwise `KEY_<BOTNAME>` (uppercase, dashes → underscores). All three provisioners route their secret writes and `$$secret(...)` placeholders through it.

### P2 — Should Do

- [ ] 2.1 Tests: schema migration round-trip (old dict shape → new list shape, both load cleanly).
- [ ] 2.2 Tests: ThreadStore bot_name disambiguation (two bots on same channel id resolve to different contexts).
- [ ] 2.3 Tests: `_default` project bootstrap creates the right folders.
- [ ] 2.4 Tests: lifecycle.start_enabled_channels with multi-bot config instantiates one adapter per bot.
- [ ] 2.5 Tests: end-to-end multi-bot dispatch (two bots, two inbound messages, each routes to its own AgentContext).
- [ ] 2.6 Docs: update `docs/channels/slack-setup.md` with multi-bot section.
- [ ] 2.7 Update spec 08 D6 secret-key naming to use the per-bot suffix convention.

### P3 — Nice to Have

- [ ] 3.1 Web UI bot card shows the bot's current Socket Mode session ID + last-seen timestamp.
- [ ] 3.2 `haz channel logs --bot hazbot` command.
- [ ] 3.3 Programmatic bot rename (changes the name field + ThreadStore migration).
- [ ] 3.4 Org-deployable / enterprise version where each bot can have a different distribution mode.

## Open Questions

- [ ] If two bots in the same workspace get @-mentioned in the same Slack channel by the same user (`@hazbot something` then `@support-bot something else`), both create their own AgentContexts — confirmed. Do we want a "shared context" mode where both bots can collaborate on the same conversation? Probably no for v1.
- [ ] When the user deletes a bot from the wizard, do we also delete the corresponding Slack app via `apps.manifest.delete`? Tempting but destructive. Probably show a confirm + a "leave it in Slack" option.
- [ ] Should `_default` project be hidden in the project picker UI, or shown? Lean toward shown (so users discover the concept) but at the bottom of the list.

## Log

**2026-05-24** — Drafted in response to user request that two bots
share the same workspace but bind to different projects. The
`_default`-as-real-project unification fell out naturally from the
same conversation: the user observed that "projectless = a project
container for chats" is a clean frame, which means making `_default`
a real entity (not a null) collapses the existing 4+ branch points
in upstream + hyperagent0 code.

**2026-05-24 (P1 implementation pass)** — Shipped P1.1–P1.8 in one
session. Strangler-fig approach with `"_legacy"` as the contract
literal across migration column DEFAULT, `LEGACY_BOT_NAME` constant,
`BaseChannel.__init__` default, `InboundMessage`/`OutboundMessage`
fields, and `_normalize_channel_key()` fallback. 178 channel + haz
tests pass (was 171); 13 new tests cover migration round-trip,
ThreadStore bot_name disambiguation, BaseChannel.bot_name, router
multi-bot dispatch, and write-back migration.

P1.9 (branch-collapse) was deferred — the four sites each have
different alternate-path semantics and aren't tractable as
one-liners without upstream `get_project_folder` honoring a
`project_folder` project.json override + per-site behavior tests.
Marked in Tasks above; follow-up session.

P1.10–P1.14 (UI + wizard + per-bot secret naming) deferred — the
foundation is complete and useful for new installs that write
list-shape channels.json from day one; old installs auto-migrate
via 1.2. UI/wizard updates are user-facing polish that can come
in a follow-up without blocking core function.

**2026-05-25 (P1.10–P1.14 shipped)** — Multi-bot UI/wizard/secret
work landed in one session:

* `/channels_status` rewritten to emit one row per
  `(channel_type, bot_name)` plus a placeholder per unconfigured
  platform. Per-bot fields (`bot_name`, `default_project`,
  `project_overrides`, `enabled`, `require_mention`,
  `allowed_users`, `allowed_chats`) ride on each row; live lookup
  honors both bare `channel_type` (legacy) and `channel_type/bot_name`.
* `channels-store.js` switched to composite cardKey
  `${channel_type}:${bot_name}`, added `isLastOfPlatform` /
  `isFirstBotOfPlatform` / `isLegacyBot` / `hasNoRealBots` /
  `_suggestBotName` helpers; test/bind controls hidden on bots
  #2+ pending per-bot endpoints.
* Channels tab template renders N cards per platform with
  bot-name suffix (`Slack — default`), per-platform
  "+ Add another <ct> bot" button, and a first-run banner that
  fires when no real bots are configured.
* Each provisioner's first input step gains a required `bot_name`
  field with help text; `dispatch.run_step` resolves bot_name
  from inputs OR a previously stashed `session.scratch["bot_name"]`
  and threads it through `ctx.bot_name`.
* `AllowlistedSecretsBridge(required_secrets, bot_name=…)` now
  computes its allow-list via the new
  `hyperagent0.channels.config.secret_key_for_bot(bot_name, key)`
  helper, which returns the bare key for `""`/`_legacy`/`default`
  and `KEY_<BOTNAME>` (uppercase, dashes → underscores) otherwise.
  All three provisioners route writes and `$$secret(...)`
  placeholders through this helper.
* `channels_config_bridge` gains
  `list_bot_names`/`read_bot_block`/`set_bot_block`. The last is
  the atomic by-name upsert provisioners use to land a new bot
  into the platform's list (normalizing legacy dict-shape on the
  fly); empty bot_name falls back to `update_block` so the CLI
  legacy path still writes dict-shape.
* `haz channel status` rewritten to iterate `(channel_type, bot_name)`,
  print bot rows like `slack/default`, and emit a
  "No bots configured. Run \`haz channel provision slack\` to add one."
  hint when no real bot is set up.

Tests: 219/219 channel+haz pass (was 210). New test files:
`tests/test_channels_status.py` (3), `tests/test_channels_config_bridge_multibot.py`
(11). Extended: `tests/test_channels_provision_slack.py` (+4 named-bot
tests). Live-verified: the standalone Slack bot at
`/tmp/slack-standalone.py` restarted cleanly under the new code
(Socket Mode `s_7708108121942` established); UI screenshot
confirms "Slack — default" card with live badge, "Add another"
button, and wizard pre-filling `bot_name=bot1` on the next bot.

P1.9 (branch-collapse for projectless / `_default` unification)
remains deferred — same reasoning as the 2026-05-24 entry.
