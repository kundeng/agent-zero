# Session note — 2026-05-24

Where things stand after the design + spec sprint and partial
implementation of spec 09.

## Bot is live with the real agent loop wired

A standalone Python script holds a Socket Mode connection to Slack as
`@hazbot` in `bayeslearner` AND now invokes the full agent loop on each
@-mention (not the echo handler from the prior session).

**Process:** `/tmp/slack-standalone.py` running under `nohup`.

**Log:** `tail -f /tmp/slack-standalone.log`

**Kill:** `pkill -f slack-standalone`

**Restart:**
```bash
OPENAI_API_KEY=local-proxy-noauth PYTHONPATH=/home/kundeng/hyperagent-zero \
  nohup .venv/bin/python /tmp/slack-standalone.py > /tmp/slack-standalone.log 2>&1 &
```

The `OPENAI_API_KEY` env var is needed even though your local proxy
doesn't validate it — LiteLLM/openai SDK insists on the variable being
set. Any value works; "local-proxy-noauth" is fine.

## ⚠️ Outstanding LLM proxy issue

Live agent test (`/tmp/test-agent-path.py`) drove a synthetic message
through the router end-to-end:

- ✅ Agent stack loads, migration runs
- ✅ ChannelRouter dispatches → AgentContext.communicate() fires
- ✅ Agent.monologue starts, prepares prompt, calls LLM
- ❌ LiteLLM call to `http://localhost:20128/v1/chat/completions`
  returned a 404 HTML page from "9Router - AI Infrastructure
  Management". Either the proxy endpoint moved, the model name
  `cc/claude-sonnet-4-6` isn't recognized, or the proxy is misconfigured.

**Action needed from you**: confirm your local proxy is up and the
endpoint/model is correct. Once it is, the @hazbot replies in Slack
will be real agent responses, not stubs.

Also: `usr/settings.json` had `workdir_path` missing, which caused an
extension to try creating `/a0/` on the host. I set it to
`/home/kundeng/hyperagent-zero/usr/workdir`. Adjust if you want a
different workdir.

## What shipped this session

Three new specs (drafts) + a real router bug fix + framework code for
spec 09. Six commits:

| SHA | What |
|---|---|
| `4cd541d` | Draft specs 09 (bot+project model), 10 (per-project capabilities), 11 (tool permissions + Claude Code parity) |
| `6762e28` | Router fix — DeferredTask.result() is async; was being discarded as coroutine (RuntimeWarning + agent reply never picked up) |
| `7ee4954` | Spec 09 P1 foundation — `_default` project bootstrap + multi-bot channels.json schema with backward-compat loader (14 new tests) |

**171 channel + haz tests pass.**

## Three new specs (drafted, not implemented beyond 09 P1 foundation)

All three drafted in detail at:
- `specs/09-bot-project-model/spec.md` — multi-bot per platform,
  `_default` as real project entity, ThreadStore key extension,
  first-run setup wizard. **Foundation pieces shipped** (`_default`
  bootstrap + `BotConfig` + `load_bot_configs()` with backward-compat).
- `specs/10-project-scoped-capabilities/spec.md` — per-project MCP
  servers (replace global), sandbox network allowlist per-project
  (layered with global default), `_default`-project skill-bridging.
  **Not yet implemented.**
- `specs/11-tool-permissions/spec.md` — Claude-Code-style permission
  model (allow/deny patterns, default mode ask/auto/bypass), six new
  native tools (Read/Edit/Write/Glob/Grep/WebFetch), ask round-trip
  via channel (reuses spec 06 D7 on_action). **Not yet implemented.**

Memory entries saved for the key design decisions so future sessions
have context without re-asking:
- `feedback_follow_upstream_silently.md` (from 2026-05-22)
- `feedback_execute_dont_inflate.md`
- `project_slack_install_models.md`
- `project_spec_scope_decisions_2026_05_22.md`

## What I corrected this session

**Skills-leak bug — was wrong.** I diagnosed `python/helpers/skills.py`
as leaking skills across projects. Re-reading: the agent-execution
path `list_skills(agent=agent)` IS correctly project-scoped via
`subagents.get_paths`. The wildcard is only used for admin views
(Web UI skill browser, CLI lister) which should see all skills.
Spec 10 D3 updated with the correction.

## Open work for next session (priority order)

### 1. Verify your LLM proxy + chat with the bot live

Trivial gating step. With the proxy working, @-mentioning the bot in
any bayeslearner channel will round-trip through the real agent loop.

### 2. Spec 09 P1.5–P1.14 — finish the multi-bot integration

Foundation is in but adapters/lifecycle/router still only see the
first bot per platform (via `load_channels_config()`'s
backward-compat wrapper). Remaining tasks:

- ThreadStore migration 002: add `bot_name` column, extend unique key
- `lifecycle.start_enabled_channels`: instantiate one adapter per bot
- `ChannelRouter`: index by `(channel_type, bot_name)`
- `BaseChannel`: add `bot_name` attribute, populated at construction
- Channels UI store: render N bot cards per platform
- Spec-08 Slack wizard: add bot-name field; secrets keyed by bot
- Branch collapse in upstream:
  `python/extensions/system_prompt/_10_system_prompt.py:75`,
  `python/tools/code_execution_tool.py:551`,
  `python/helpers/secrets.py:get_secrets_manager`,
  `hyperagent0/sandbox/srt.py:_ensure_profile` —
  each can now use `resolve_project_name()` and drop its `if project_name:`
  branch
- First-run nag in `webui/components/settings/channels/channels.html`
- `haz status` hint when no bots configured

### 3. Spec 10 P1 — per-project MCP + sandbox network

- `usr/projects/<name>/.a0proj/mcp_servers.json` loader, **replaces**
  global when present
- `srt.py:_ensure_profile` reads project.json `network.allow` +
  global `sandbox_network_default`, writes union
- Knowledge subdir wiring verification
- UI editors per project

### 4. Spec 11 P1 — permission model + Claude Code parity tools

- `hyperagent0/permissions.py`: file loader, pattern compilation
  cache, `resolve_decision(tool_name, args, project) → mode`
- Hook in `tool_execute_before` extension folder
- Six new tools: Read, Write, Edit, Glob, Grep, WebFetch
- Channel ask round-trip via spec 06 D7 `on_action`
- Default-permissions bootstrap

### 5. Daemon vs standalone `invalid_auth` issue (spec 08 P2.5.2)

Still unsolved. Standalone runtime works; full daemon poisons
slack-bolt's HTTP. The `haz channel run` standalone-mode command
(spec 08 P2.5.3) would workaround it production-quality but doesn't
fix root cause.

## Things you can do right now to play

- `@hazbot hello` in any channel of bayeslearner (needs `/invite @hazbot`
  first if not already in the channel) — once the LLM proxy is fixed,
  responses come from the real agent loop.
- DM `@hazbot` directly — bot has `im:write` scope.
- Tail the log live: `tail -f /tmp/slack-standalone.log`
- Browse the new specs: `specs/09-bot-project-model/spec.md`,
  `specs/10-project-scoped-capabilities/spec.md`,
  `specs/11-tool-permissions/spec.md`

## Project structure as it stands

- **Branch**: `v2-hyperagent`. Last 4 commits this session:
  `7ee4954`, `6762e28`, `4cd541d` (specs), then prior session's
  `f464c39` (Slack runbook docs).
- **Tests**: 171 channel + haz tests pass.
- **Specs status**:
  - 01 SHIPPED, 02 DRAFT, 03 SHIPPED, 04 SHIPPED, 05 WITHDRAWN,
    06 SHIPPED (D7 P2.1 still open), 07 SHIPPED
  - 08 PARTIAL (D10 pivot pending — `apps.manifest.create` creates
    orphan apps; rework wizard to paste-manifest)
  - 09 DRAFT (P1 foundation: `_default` + `BotConfig` schema shipped)
  - 10 DRAFT (no code yet)
  - 11 DRAFT (no code yet)
- **Slack apps in bayeslearner**: A0B5V01R4TE (the working one,
  created via UI from our manifest). Earlier orphans all deleted.
