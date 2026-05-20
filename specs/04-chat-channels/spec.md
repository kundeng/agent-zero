---
spec_id: 04-chat-channels
status: DRAFT
since: 2026-05-20
until: null
epic: channels
features: [channel-adapter-interface, telegram-adapter, slack-adapter, discord-adapter, channel-router]
supersedes: []
superseded_by: null
depends_on: [01-host-first, 03-daemon-cli]
---

# Chat Channel Integration

## Context

Agent Zero is web-UI-only. You interact with it through a Flask web app served on localhost. For a hyperagent harness, you need to reach it from wherever you already are: Slack, Telegram, Discord, WhatsApp, etc.

NanoClaw solves this with a channel adapter registry and per-channel routing. Rather than rebuilding NanoClaw's full 25+ channel ecosystem, we build a clean adapter interface and implement three priority channels natively in Python.

### Relationship to spec 01 (wrapper architecture) and spec 03 (daemon)

Per spec 01 D9, all net-new code lives under `hyperagent0/`. This spec's home is `hyperagent0/channels/`:

```
hyperagent0/channels/
├── __init__.py             # registry, BaseChannel ABC
├── base.py                 # InboundMessage, OutboundMessage dataclasses
├── router.py               # channel→AgentContext mapping (SQLite-backed)
├── formatter.py            # markdown→platform formatter
├── config.py               # channel config schema + loader
├── telegram.py             # python-telegram-bot adapter
├── slack.py                # slack-bolt adapter (P2)
└── discord.py              # discord.py adapter (P2)
```

**Lifecycle (per spec 03 D5/D6).** Channel adapters run as in-process async tasks inside the daemon. `hyperagent0/daemon.py` boots channels at start, drains them on shutdown. Channels are *not* started from `run_ui.py` — that file stays unchanged. If a user runs the legacy `python run_ui.py` directly (not through `haz start`), channels stay off (web UI only).

**Conflict-surface budget for spec 04**: ideally **zero** upstream patches. Channel config lives in a new `hyperagent0/channels/config.py` (or extends spec 01's per-project schema in `project.json`), not in `python/helpers/settings.py`. The `$$secret()` placeholder mechanism is reused as-is (no changes to its implementation).

## Constraints

- Python async (no Node.js sidecar for Phase 1)
- Each channel adapter is a standalone async class — can be enabled/disabled independently
- Channel secrets use Agent Zero's existing `$$secret()` mechanism
- Message routing must handle concurrent inbound from multiple channels
- Channel→context mapping must persist across restarts (SQLite at `~/.hyperagent0/channels.db`)
- Channel-library deps (`python-telegram-bot`, `slack-bolt`, `discord.py`) install via `[telegram]`, `[slack]`, `[discord]`, or `[channels]` extras (spec 01 D7) — not in base `requirements.txt`
- Lazy imports: a channel module imports its SDK only when its adapter class is instantiated
- Channels run inside the daemon process (spec 03); they do not start from `run_ui.py`

## Decisions

### D1: Python-native adapters, not NanoClaw Chat SDK bridge
**Choice**: Write Telegram, Slack, Discord adapters in Python.
**Why**: Avoids Node.js sidecar. Three initial adapters are straightforward with `python-telegram-bot`, `slack-bolt`, `discord.py`. Consider NanoClaw Chat SDK bridge as Phase 2 for remaining 20+ platforms.

### D2: BaseChannel abstract class
**Choice**: Common interface: `connect()`, `disconnect()`, `send()`, `on_message()` callback.
**Why**: Clean adapter pattern. New channels = one new class implementing BaseChannel.

### D3: SQLite for channel→context persistence
**Choice**: Store channel/thread → AgentContext mapping in SQLite.
**Why**: Survives restarts. Agent Zero already uses SQLite patterns. No new dependency.

### D4: Markdown→platform formatter
**Choice**: Central formatter converts agent markdown output to platform-specific markup.
**Why**: Agent responses contain markdown, code blocks, file paths. Each platform (Slack blocks, Telegram HTML, Discord markdown) needs different formatting.

## Tasks

### P1 — Must Do
- [ ] 1.1 Create `hyperagent0/channels/__init__.py` and `hyperagent0/channels/base.py`
  - `BaseChannel` ABC with `connect()`, `disconnect()`, `send()`, `on_message()` callback
  - `InboundMessage` and `OutboundMessage` dataclasses
  - Channel registry (discover and instantiate enabled channels)
- [ ] 1.2 Create `hyperagent0/channels/router.py`
  - Map inbound messages to AgentContexts
  - Create new context for new conversations, resume for existing threads
  - SQLite persistence at `~/.hyperagent0/channels.db`; schema: `(channel_type, chat_id, context_id, project_name, last_active)`
  - When creating a new context, activate the project bound to that channel/chat in config (if any)
- [ ] 1.3 Implement Telegram adapter (`hyperagent0/channels/telegram.py`)
  - Lazy import: `from telegram import ...` inside the adapter class, not at module top level
  - Bot token from `$$secret()` placeholder in channel config
  - Allowed users whitelist
  - Thread mapping: Telegram chat_id → AgentContext
- [ ] 1.4 Create `hyperagent0/channels/formatter.py`
  - Markdown → Telegram HTML
  - Markdown → Slack blocks (placeholder for P2)
  - Markdown → Discord markdown (mostly passthrough, placeholder for P2)
  - Code block handling for each platform
- [ ] 1.5 Create `hyperagent0/channels/config.py` for channel configuration
  - Schema: per-channel `enabled`, `token` (via `$$secret()`), `allowed_users`/`allowed_chats`, `project_binding` (chat → project name)
  - Loaded from a new `channels.json` or from a `channels` section in existing settings; no patch to `python/helpers/settings.py` if possible
- [ ] 1.6 Boot channels from the daemon lifecycle
  - `hyperagent0/daemon.py` (spec 03) starts enabled channel adapters at startup
  - Graceful disconnect on SIGTERM (uses spec 03 task 1.6 shutdown handler)
  - Does **not** touch `run_ui.py` — direct `python run_ui.py` runs continue to be web-UI-only

### P2 — Should Do
- [ ] 2.1 Implement Slack Socket Mode adapter (`hyperagent0/channels/slack.py`)
  - Lazy import `slack_bolt`
  - App token + bot token from channel config
  - Thread mapping: Slack thread_ts → AgentContext
- [ ] 2.2 Implement Discord adapter (`hyperagent0/channels/discord.py`)
  - Lazy import `discord`
  - Bot token from channel config
  - Guild/channel allowlists
- [ ] 2.3 Test: Telegram end-to-end (message → agent → reply)
- [ ] 2.4 Test: Multi-channel concurrent messages
- [ ] 2.5 Test: Context resume after daemon restart

### P3 — Nice to Have
- [ ] 3.1 File/image attachment support in channel messages
- [ ] 3.2 Evaluate NanoClaw Chat SDK bridge for Phase 2 (20+ additional channels)
- [ ] 3.3 Channel-aware agent profiles (different behavior for Slack vs Telegram)

## Open Questions

- [ ] Should channel messages go to the same AgentContext as web UI, or separate contexts? Likely separate — different conversation threads.
- [ ] Rate limiting on inbound channel messages? Probably needed to prevent abuse from public channels.
- [ ] Should the agent be able to proactively send to channels (not just reply)? Yes, needed for scheduled task notifications.

## Log

**2026-05-20** — Initial spec. Reviewed NanoClaw's channel architecture (two-DB split, channel adapter registry). Decided against Chat SDK bridge for Phase 1 to avoid Node.js sidecar. Three priority adapters: Telegram, Slack, Discord.

**2026-05-20** — Aligned with spec 01 D9 (wrapper architecture) and spec 03 (daemon lifecycle). All channel code moves from `python/channels/` to `hyperagent0/channels/`. Channel startup lives in `hyperagent0/daemon.py`, **not** `run_ui.py` — direct `python run_ui.py` invocations stay web-UI-only. Channel config moves to its own module (`hyperagent0/channels/config.py`) to keep the spec-04 conflict-surface budget at zero upstream patches. Channel-library deps install via spec 01 D7 extras (`[telegram]`, `[slack]`, `[discord]`, or `[channels]` bundle). SQLite mapping DB lives at `~/.hyperagent0/channels.db`.
