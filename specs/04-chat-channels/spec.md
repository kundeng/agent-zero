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

## Constraints

- Python async (no Node.js sidecar for Phase 1)
- Each channel adapter is a standalone async class — can be enabled/disabled independently
- Channel secrets use Agent Zero's existing `$$secret()` mechanism
- Message routing must handle concurrent inbound from multiple channels
- Channel→context mapping must persist across restarts (SQLite)

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
- [ ] 1.1 Create `python/channels/__init__.py` and `python/channels/base.py`
  - `BaseChannel` ABC with connect/disconnect/send/on_message
  - `InboundMessage` and `OutboundMessage` dataclasses
  - Channel registry (discover and instantiate enabled channels)
- [ ] 1.2 Create `python/channels/router.py`
  - Map inbound messages to AgentContexts
  - Create new context for new conversations, resume for existing threads
  - SQLite persistence for channel→context mapping
- [ ] 1.3 Implement Telegram adapter (`python/channels/telegram.py`)
  - Uses `python-telegram-bot` library
  - Bot token from settings/secrets
  - Allowed users whitelist
  - Thread mapping: Telegram chat_id → AgentContext
- [ ] 1.4 Create `python/channels/formatter.py`
  - Markdown → Telegram HTML
  - Markdown → Slack blocks
  - Markdown → Discord markdown (mostly passthrough)
  - Code block handling for each platform
- [ ] 1.5 Add channel config section to `settings.py`
  - Per-channel: enabled, token, allowed_users/channels
  - [src:python/helpers/settings.py]
- [ ] 1.6 Wire channel startup into server lifecycle
  - Start channel adapters when daemon starts
  - Graceful disconnect on shutdown
  - [src:run_ui.py]

### P2 — Should Do
- [ ] 2.1 Implement Slack Socket Mode adapter (`python/channels/slack.py`)
  - Uses `slack-bolt` library
  - App token + bot token from settings
  - Thread mapping: Slack thread_ts → AgentContext
- [ ] 2.2 Implement Discord adapter (`python/channels/discord.py`)
  - Uses `discord.py` library
  - Bot token from settings
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
