---
spec_id: 06-channel-hardening
status: DRAFT
since: 2026-05-20
until: null
epic: channels
features: [reply-to-routing, mention-signal, network-retry, on-action-callback, channel-sandbox-bridge, sqlite-migrations]
supersedes: []
superseded_by: null
depends_on: [04-chat-channels]
---

# Channel Hardening (port NanoClaw patterns)

## Context

Spec 04 shipped a working Phase-1 channel layer: `BaseChannel` ABC, `ChannelRouter`, Telegram/Slack/Discord adapters, formatter, SQLite-backed thread store, lifecycle hook into the daemon. All P1 + most P2 done, 29 tests passing, zero `python/` patches.

But spec 04 greenfielded interfaces that NanoClaw already solved through production traffic. The gaps surface as real bugs the first time the system meets real users:

1. **Telegram bot-mention detection by agent-name regex** — fails immediately. Telegram doesn't surface the agent's display name (`@Andy`) in message text; it surfaces the bot's platform username (`@yourbot_v2_refactr_1_bot`). The platform DOES send a structured "this is a mention of the bot" signal that we ignore.
2. **Reply always goes to the inbound channel** — there is no way to drive the agent from a CLI/admin tool and have replies land somewhere else. Every NanoClaw operator hits this in week one.
3. **A DNS hiccup at boot kills a channel permanently** — Telegram's `deleteWebhook` call at adapter setup can timeout on a slow network; our adapter throws and the channel is dead until daemon restart. NanoClaw retries 3 times with backoff on `NetworkError` only.
4. **No interactive UI plumbing** — the agent's `ask_question` flow on text-only platforms degrades to free-text. NanoClaw wires an `onAction(question_id, selected_option, user_id)` callback so users click buttons in Telegram/Slack/Discord cards.
5. **No channel↔sandbox config bridge** — a Telegram chat bound to project X should automatically inherit project X's `sandbox_mode`, resource limits, and FS-mount config. We resolve the project but never thread its sandbox config through.
6. **Raw `CREATE TABLE IF NOT EXISTS`** — works for v1, will collide when the schema needs to add a column.

This spec ports the NanoClaw interface design (TypeScript) to Python, restructures spec 04's ABC, and bridges to spec 05's sandbox config.

## Constraints

- No upstream `python/` patches beyond what spec 04 already established (zero) — all changes contained in `hyperagent0/channels/`
- Backward-compatible at the daemon boot path: `hyperagent0/cli_commands/start.py` and `hyperagent0/shutdown.py` should not need changes (the lifecycle module owns adapter init/teardown)
- All three shipped adapters (Telegram, Slack, Discord) update simultaneously — no half-migration
- Lazy-import contract preserved: importing `hyperagent0.channels.*` must not pull `python-telegram-bot`, `slack-bolt`, or `discord.py`
- Existing 29 channel tests continue to pass; new tests added for the new interfaces

## Decisions

### D1: Split routing from content — `InboundEvent` + `InboundMessage`
**Choice**: New `InboundEvent` dataclass carries `channel_type`, `platform_id`, `thread_id`, `reply_to` (optional `DeliveryAddress`), and `message: InboundMessage`. The message dataclass keeps content fields (`id`, `kind`, `content`, `timestamp`, `is_mention`, `is_group`).
**Why**: A clean way to add `reply_to` without polluting the message body. `is_mention` and `is_group` are platform metadata, not content — keeping them in message is fine but the routing fields belong outside.

### D2: `ChannelSetup` callback bundle (replaces single `on_message`)
**Choice**: Adapters call a `setup(setup: ChannelSetup)` method at boot. `ChannelSetup` exposes four callbacks the host implements:
- `on_inbound(platform_id, thread_id, message)` — normal chat path
- `on_inbound_event(event)` — admin-transport path that can set `reply_to`
- `on_metadata(platform_id, name=None, is_group=None)` — adapter discovered chat metadata
- `on_action(question_id, selected_option, user_id)` — user clicked an interactive button

**Why**: Mirrors NanoClaw. Lets the host distinguish "normal user message" from "admin asks the agent something" from "user clicked button on an open ask-question card." Adapters that don't support a channel just don't fire it.

### D3: Platform-confirmed `is_mention` overrides regex
**Choice**: Adapters set `InboundMessage.is_mention=True` from the platform's structured mention signal:
- Telegram: scan `message.entities` for `type='mention'` or `type='text_mention'` resolving to the bot's `username`/`user_id`; for non-text messages, fall back to the `via_bot` field
- Slack: `app_mention` event type sets True; thread `parent_user_id == bot_id` sets True
- Discord: `message.mentions.bot.id == client.user.id`

Router checks `is_mention` first; falls back to agent-name match only if the platform didn't provide a signal (web channel, custom integrations).

**Why**: Direct fix for the Telegram bug. NanoClaw's comment on this in `adapter.ts:60-72` documents the exact failure case.

### D4: Retry adapter `setup()` on `NetworkError` only
**Choice**: Wrap each adapter's `setup()` call in `hyperagent0/channels/lifecycle.py` with retry: 3 attempts at 2s/5s/10s, only when the exception is a network failure. Misconfigs (invalid token, missing intent) fail fast on the first attempt.

Network-failure detection per SDK:
- Telegram: `telegram.error.NetworkError`, `telegram.error.TimedOut`
- Slack: `slack_sdk.errors.SlackApiError` where `response.status_code in (429, 502, 503, 504)`
- Discord: `discord.HTTPException` where `status in (429, 500, 502, 503, 504)`, `discord.ConnectionClosed`
- Generic: `OSError`, `ConnectionError`, `TimeoutError`, `asyncio.TimeoutError`, `socket.gaierror`

**Why**: Operational reliability. A boot-time DNS flake must not require manual daemon restart.

### D5: ~~Channel↔sandbox config bridge~~ — WITHDRAWN 2026-05-22
Removed when spec 05 was withdrawn. Sandbox mode is now a single global
setting and there is no per-channel override. `ChannelConfig.sandbox_override`,
`ChannelRouter._apply_sandbox_override`, and the three associated
`test_sandbox_override_*` tests were deleted in the same retraction.

### D6: SQLite migrations
**Choice**: Replace raw `CREATE TABLE IF NOT EXISTS` with a numbered-migration system. New `hyperagent0/channels/migrations/` directory holds `001_initial.sql`, `002_*.sql`, etc. A `Migrator` class records applied versions in a `schema_migrations` table and applies missing ones at `ThreadStore` open.

**Why**: When spec 06 itself needs to add `reply_to_default` or future schema additions, we won't have to write defensive code or break existing installations. NanoClaw's pattern (`012-channel-registration.ts`) is the model.

### D7: `on_action` integration with upstream's `ask_question`
**Choice**: Add `python/extensions/ask_question_before/` (NEW upstream extension hook) — wait, that's an upstream patch. Instead: the router's `on_action` callback updates the `AgentContext` task state directly via the existing public API (`context.intervention.set_response(...)` or equivalent — to be confirmed during implementation). No agent.py patch.

**Why**: Keep the conflict-surface budget at zero for spec 06. Pay closer attention during implementation: if no existing public API can deliver the answer, this decision needs to be revisited and we file an open question for either an upstream patch or an extension.

## Tasks

### P1 — Must Do
- [x] 1.1 New dataclasses in `hyperagent0/channels/base.py`:
  - `DeliveryAddress`, `InboundEvent`, `ChannelSetup` ABC, `InboundMessage` augmented with `is_mention` / `is_group` / `kind`
  - [src:hyperagent0/channels/base.py:92-174]
- [x] 1.2 Update `BaseChannel`:
  - `setup(channel_setup)` shipped; legacy `on_message` accepted via shim in `_dispatch_inbound`
  - [src:hyperagent0/channels/base.py:212-280]
- [x] 1.3 Update `ChannelRouter`:
  - All four `ChannelSetup` callbacks implemented (`on_inbound`, `on_inbound_event`, `on_metadata`, `on_action`)
  - `require_mention` gate enforces `is_mention` in groups; DMs always pass
  - `event.reply_to` redirects reply to a different registered adapter (tested)
- [x] 1.4 Retry-on-NetworkError around `adapter.setup()` (D4) — `hyperagent0/channels/lifecycle.py`
  - `is_network_error()` discriminator covers stdlib transients + Telegram/Slack/Discord network exception classes
- [x] 1.5 Migration system (D6) — `hyperagent0/channels/migrations/{001_initial.sql, migrator.py}`
  - `ThreadStore.__init__` invokes `Migrator.upgrade()`; idempotent re-runs verified
- [~] 1.6 ~~Channel↔sandbox bridge (D5)~~ — withdrawn 2026-05-22 along with spec 05. Code removed.
- [x] 1.7 Update Telegram adapter
  - `setup()` shipped; `_detect_is_mention()` scans `message.entities` for `mention` / `text_mention` resolving to the bot's username
- [x] 1.8 Update Slack adapter
  - `setup()` shipped; `app_mention` event + thread `parent_user_id == bot_id` set `is_mention=True`
- [x] 1.9 Update Discord adapter
  - `setup()` shipped; `message.mentions` drives `is_mention`

### P2 — Should Do
- [ ] 2.1 `on_action` callback for one platform (Telegram inline keyboard buttons)
  - `ChannelSetup.on_action` callback wired on the host side, but **no adapter emits the buttons or fires the callback yet** — grepping the three adapter files for `inline_keyboard` / `InlineKeyboardButton` returns nothing. This is the one genuine P1-adjacent gap remaining in spec 06
- [x] 2.2 Test: `is_mention` detection
  - `test_require_mention_blocks_unmentioned_group`, `_passes_mentioned_group`, `_does_not_block_dms`
- [x] 2.3 Test: `reply_to` routing
  - `test_reply_to_redirects_to_different_adapter`, `test_no_reply_to_lands_on_inbound_channel`
- [x] 2.4 Test: `NetworkError` retry
  - `test_is_network_error_true_for_stdlib_transients` + `_false_for_misconfigs`
- [x] 2.5 Test: migration upgrade
  - `test_migrator_applies_initial_then_idempotent`, `test_thread_store_uses_migrator`

### P3 — Nice to Have
- [ ] 3.1 `on_action` for Slack (block_actions) and Discord (component interactions)
- [ ] 3.2 `channel-approval` flow — admin must approve new (channel, user) pairs before agent responds
- [ ] 3.3 Chat SDK bridge (NanoClaw's `@chat-adapter/shared` equivalent in Python) — defer until P1/P2 prove the new interface is right

## Open Questions

- [ ] D7 (`on_action` ↔ `ask_question`): does upstream expose a public way to deliver an answer to a pending `ask_question` from outside the agent loop? If not, this becomes either an upstream patch (one new file in `python/extensions/`) or a router-side state hack. Need to grep `python/tools/ask_question*` during implementation.
- [ ] Should `reply_to` be settable by agents (not just admin transports)? NanoClaw says no — `replyTo` is router-layer operator intent only. We probably want the same.
- [ ] `is_mention` for the web UI (Flask) channel — does it even apply? Web UI sends through the existing socket-io path, not the channel abstraction. Probably orthogonal.

## Log

**2026-05-20** — Drafted after spec-04 audit against `/home/kundeng/hyperagent-eval/nanoclaw/src/channels/`. Key references: `adapter.ts:1-100` (the `ChannelSetup` and `InboundEvent` shapes), `channel-registry.ts:55-95` (the retry-on-NetworkError pattern), `db/migrations/012-channel-registration.ts` (migration model). Conflict-surface budget for this spec: **zero** upstream patches in `python/` (D7 may revise this).

**2026-05-22** — Audit pass against committed code. **All P1 (1.1–1.9) shipped** — `DeliveryAddress` / `InboundEvent` / `ChannelSetup` dataclasses in `base.py`, the four router callbacks, `require_mention` enforcement, `is_network_error()` discriminator + retry loop in `lifecycle.py`, numbered SQL migrations in `channels/migrations/`, `sandbox_override` bridge to `AgentConfig.additional`, and all three adapters detect `is_mention` from platform-structured signals. Test file `tests/test_hyperagent0_channels_spec06.py` covers every D-decision (D1–D6) explicitly. **P2 4 of 5 shipped**: 2.2 (is_mention), 2.3 (reply_to), 2.4 (network error), 2.5 (migration upgrade) all have tests. Only **2.1 (`on_action` adapter wire-up)** remains — the host-side callback exists in `ChannelSetup` but no adapter emits inline buttons or invokes the callback. D7 (the `ask_question` integration) stays an Open Question — would only become real once 2.1 is wired. Conflict-surface budget honored: zero upstream `python/` patches.

**2026-05-22 (later)** — **D5 (channel↔sandbox bridge) withdrawn** in lockstep with spec 05 retraction. `ChannelConfig.sandbox_override` field removed, `ChannelRouter._apply_sandbox_override` removed, and the three `test_sandbox_override_*` tests deleted from `test_hyperagent0_channels_spec06.py`. D1–D4 + D6 remain shipped and tested. Spec 06 surface area shrinks but stays cleanly bounded.
