---
title: "Chat Channel Integration"
status: draft
priority: P2
breaks_compat: true
depends_on: ["001-host-first-architecture", "003-daemon-cli"]
---

# Spec 004: Chat Channel Integration

## Problem

Agent Zero is web-UI-only. You interact with it through a Flask web app served on localhost. For a hyperagent harness, you need to reach it from wherever you already are: Slack, Telegram, Discord, WhatsApp, etc.

NanoClaw solves this with a channel adapter registry and per-channel routing. Rather than rebuilding this from scratch, we should adopt NanoClaw's Chat SDK bridge pattern or directly integrate their channel adapters.

## Requirements

### R1: Channel adapter abstraction
- A `BaseChannel` interface that all messaging platforms implement
- Standard lifecycle: `connect()`, `disconnect()`, `send_message()`, `on_message()` callback
- Channel-specific config (bot tokens, webhook URLs) stored in settings

### R2: Initial channel support (Phase 1)
- **Telegram** — most common for personal agents, bot API is simple
- **Slack** — most common for work, Socket Mode for no public endpoint
- **Discord** — common for dev communities

### R3: Message routing
- Inbound message → identify channel + sender → find or create AgentContext → inject as `UserMessage`
- Outbound agent response → format for channel (markdown → Slack blocks, Telegram HTML, etc.) → send
- Support multi-turn: channel thread/conversation maps to a persistent AgentContext

### R4: NanoClaw Chat SDK compatibility (stretch goal)
- If NanoClaw's Chat SDK is published as an npm package, consider bridging to it via a sidecar process
- This gives access to their full channel adapter library (25+ platforms) without reimplementing

## Design

### Architecture

```
┌─────────────────────────────────────────────┐
│                HyperAgent Zero              │
│                                             │
│  ┌──────────┐   ┌──────────┐   ┌────────┐  │
│  │ Web UI   │   │ Channels │   │  CLI   │  │
│  │ (Socket  │   │ Router   │   │        │  │
│  │  IO)     │   │          │   │        │  │
│  └────┬─────┘   └────┬─────┘   └───┬────┘  │
│       │              │              │       │
│       └──────────────┼──────────────┘       │
│                      │                      │
│              ┌───────▼────────┐             │
│              │  AgentContext   │             │
│              │  Manager       │             │
│              └───────┬────────┘             │
│                      │                      │
│              ┌───────▼────────┐             │
│              │  Agent Loop    │             │
│              └────────────────┘             │
└─────────────────────────────────────────────┘
```

### Channel adapter interface

```python
# python/channels/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class InboundMessage:
    channel_type: str           # "telegram", "slack", "discord"
    channel_id: str             # platform-specific channel/chat ID
    sender_id: str              # platform-specific user ID
    sender_name: str
    text: str
    thread_id: str | None       # for threaded conversations
    attachments: list[bytes]    # images, files

@dataclass
class OutboundMessage:
    text: str                   # markdown
    channel_id: str
    thread_id: str | None
    attachments: list[tuple[str, bytes]]  # (filename, data)

class BaseChannel(ABC):
    @abstractmethod
    async def connect(self) -> None: ...
    
    @abstractmethod
    async def disconnect(self) -> None: ...
    
    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None: ...
    
    def set_message_handler(self, handler: Callable[[InboundMessage], Awaitable[None]]):
        self._handler = handler
```

### Message routing

```python
# python/channels/router.py
class ChannelRouter:
    """Maps channel messages to AgentContexts."""
    
    def __init__(self, context_manager: AgentContextManager):
        self._contexts = context_manager
        self._channel_map: dict[str, str] = {}  # "telegram:12345" → context_id
    
    async def route_inbound(self, msg: InboundMessage):
        key = f"{msg.channel_type}:{msg.channel_id}"
        if msg.thread_id:
            key += f":{msg.thread_id}"
        
        context_id = self._channel_map.get(key)
        if not context_id:
            context_id = self._contexts.create(source=key)
            self._channel_map[key] = context_id
        
        await self._contexts.send_message(context_id, msg.text, msg.sender_name)
    
    async def route_outbound(self, context_id: str, text: str):
        # Find which channel this context is associated with
        # Format text for that channel's markup
        # Send via the channel adapter
        ...
```

### Key files

| File | Purpose |
|------|---------|
| NEW: `python/channels/__init__.py` | Channel registry |
| NEW: `python/channels/base.py` | `BaseChannel`, `InboundMessage`, `OutboundMessage` |
| NEW: `python/channels/router.py` | Message routing, context mapping |
| NEW: `python/channels/telegram.py` | Telegram bot adapter |
| NEW: `python/channels/slack.py` | Slack Socket Mode adapter |
| NEW: `python/channels/discord.py` | Discord bot adapter |
| NEW: `python/channels/formatter.py` | Markdown → platform-specific markup |
| `python/helpers/settings.py` | Channel config section |
| `run_ui.py` | Start channel adapters alongside web server |

### Configuration

```json
{
    "channels": {
        "telegram": {
            "enabled": true,
            "bot_token": "$$secret(telegram_bot_token)",
            "allowed_users": [123456789]
        },
        "slack": {
            "enabled": false,
            "app_token": "$$secret(slack_app_token)",
            "bot_token": "$$secret(slack_bot_token)",
            "allowed_channels": ["C01234ABCDE"]
        },
        "discord": {
            "enabled": false,
            "bot_token": "$$secret(discord_bot_token)",
            "allowed_guilds": ["123456789012345678"]
        }
    }
}
```

## Risks

- **Security**: Chat channels are internet-facing. Must validate sender identity, enforce allowed_users/channels, rate-limit inbound messages.
- **Message formatting**: Agent responses contain markdown, code blocks, file paths. Each platform has different formatting support. The formatter must degrade gracefully.
- **Concurrency**: Multiple channels can send messages simultaneously. The router must handle concurrent context creation without races.
- **State persistence**: Channel → context mapping must survive restarts. Store in SQLite alongside agent state.

## Tasks

- [ ] Design `BaseChannel` interface
- [ ] Implement `ChannelRouter` with context mapping
- [ ] Implement Telegram adapter (Phase 1 priority)
- [ ] Implement Slack Socket Mode adapter
- [ ] Implement Discord adapter
- [ ] Create `formatter.py` for markdown → platform markup
- [ ] Add channel config to settings.py
- [ ] Wire channel startup into `run_ui.py` / daemon lifecycle
- [ ] Persist channel→context mapping in SQLite
- [ ] Test: Telegram end-to-end (send message → agent responds → reply in Telegram)
- [ ] Test: Multi-channel concurrent messages
- [ ] Test: Context resume after restart
- [ ] Evaluate NanoClaw Chat SDK bridge as Phase 2
