# Session note — 2026-05-24 (continuation)

Picked up the previous session note and marked through its priority
list. Three new commits, 182/182 tests pass, spec 09 frontmatter
flipped DRAFT → PARTIAL.

## What got verified

**LLM proxy works.** `cc/claude-sonnet-4-6` at `localhost:20128`
streams chat completions cleanly. The 404 the previous session saw
was either transient or model-name-related; current setup is fine.
The standalone Slack bot (`/tmp/slack-standalone.py`, PID 2302220) is
still up and waiting for @-mentions. Restart command from the prior
note is unchanged.

## What shipped this session

| SHA | What |
|---|---|
| `f56b905` | Spec 10 D3 correction committed (housekeeping) |
| `58eee22` | Spec 09 P1.3–P1.7: multi-bot lifecycle + (channel_type, bot_name) routing |
| `538214b` | Spec 09 P1.2: write-back migration of legacy channels.json |

**Spec 09 P1.1–P1.8 are now all SHIPPED** (status PARTIAL because
P1.9+ deferred). Foundation supports two bots on the same Slack
workspace, each binding to a different project — the original user
request that drove the spec.

## Architecture summary of what landed

- **Migration 002** (`hyperagent0/channels/migrations/002_bot_name.sql`):
  extends `thread_map` PK from `(channel_type, chat_id)` to
  `(channel_type, bot_name, chat_id)`. SQLite-safe rebuild via temp
  table swap inside BEGIN/COMMIT. Existing rows keep working as
  `bot_name='_legacy'`.

- **ThreadStore** signatures take `bot_name` kwarg (default
  `"_legacy"` matches the migration column default). Composite key
  is the 3-tuple everywhere.

- **`BaseChannel.bot_name`** attribute set at construction. The
  three adapters (Slack/Telegram/Discord) propagate it and stamp it
  on the `InboundMessage`s they build.

- **`InboundMessage` and `OutboundMessage`** dataclasses gained
  `bot_name: str = "_legacy"` fields so routing key is traceable
  end-to-end.

- **`ChannelRouter`** keyed by `(channel_type, bot_name)` everywhere
  — adapters, configs, store calls, reply path. New helper
  `_normalize_channel_key()` lets pre-spec-09 callers pass plain
  `str` keys (wrapped as `(str, "_legacy")`) so all existing tests
  ran unchanged.

- **`lifecycle.start_enabled_channels`** iterates `load_bot_configs()`,
  instantiates one adapter per enabled bot, registers each under
  `(channel_type, bot_name)`. `running_adapters()` reports bare
  `channel_type` for `_legacy` (single-bot) installs so existing
  status UI consumers (`/channels_status`, `haz channel status`)
  keep working without UI-side changes.

- **`load_bot_configs(persist_migration=True)`** writes the
  normalized list-shape back to `~/.hyperagent0/channels.json` on
  first load if the file was in old dict-shape. Atomic via tempfile
  + os.replace. `usr/settings.json` is never written (read-only
  layer).

**The contract literal is `"_legacy"`**, used in 6 places (migration
column DEFAULT, `LEGACY_BOT_NAME` constant, `BaseChannel.__init__`
default, two dataclass field defaults, `_normalize_channel_key`
fallback). If anyone refactors and the literal drifts, single-bot
installs would silently fork off a new row family separate from
their migrated history.

## What's deferred

### Spec 09 P1.9 — branch-collapse in upstream (DEFERRED)

The session-note-prior promise of "minimal one-liner each" doesn't
survive a close read of the call sites:

1. `python/extensions/system_prompt/_10_system_prompt.py:75` — picks
   between `projects.active.md` and `projects.inactive.md` templates.
   Different prompt text in each branch.
2. `python/tools/code_execution_tool.py:551` — falls back to
   `settings.workdir_path` when no project. Cwd would change without
   work to make `_default.project_folder = workdir_path` respected.
3. `python/helpers/secrets.py:get_secrets_manager` — relatively safe;
   appends per-project secrets.env (SecretsManager skips missing files).
4. `hyperagent0/sandbox/srt.py:_ensure_profile` — sets sandbox
   `fs.write.allow` to project_dir. For `_default`, that's
   `usr/projects/_default/`.

True collapse needs upstream `get_project_folder` to honor a
`project_folder` override in `project.json` + per-site behavior
tests. Logged in spec 09 task list as DEFERRED 2026-05-24.

### Spec 09 P1.10–P1.14 (UI / wizard / per-bot secret naming)

Foundation is complete and useful — new installs write list-shape
channels.json from day one; old installs auto-migrate. UI/wizard
updates are user-facing polish that can come in a follow-up without
blocking core function.

## Open work for next session (priority order)

### 1. Live-test the bot

Proxy is verified, foundation is wired. `@hazbot hello` in any
`bayeslearner` channel should round-trip through the real agent loop
now. If you see anything odd, the log is at
`/tmp/slack-standalone.log`.

Worth a restart if the standalone bot has been idle for hours:
```bash
pkill -f slack-standalone
OPENAI_API_KEY=local-proxy-noauth PYTHONPATH=/home/kundeng/hyperagent-zero \
  nohup .venv/bin/python /tmp/slack-standalone.py > /tmp/slack-standalone.log 2>&1 &
```

### 2. Spec 09 P1.9 (branch-collapse) — proper version

Now that the foundation is in, the branch-collapse becomes a focused
piece of work. Sequencing:
- Modify upstream `python/helpers/projects.py:get_project_folder` to
  honor a `project_folder` override in `project.json`.
- In `ensure_default_project`, set `project_folder` to the current
  `settings.workdir_path` so `_default` cwd matches legacy behavior.
- Add per-site behavior tests covering each of the 4 branches.
- Then collapse the conditionals to single `resolve_project_name()`
  calls.

### 3. Spec 10 P1 — per-project MCP + sandbox network

Foundation in spec 09 made this tractable. `BotConfig.default_project`
+ `project_overrides` already resolve every inbound to a project
name; spec 10 P1 builds on that with per-project capability files.

### 4. Spec 11 P1 — permission model + Claude Code parity tools

Independent of 9 and 10. Can be picked up any time.

### 5. Daemon vs standalone `invalid_auth` issue (spec 08 P2.5.2)

Still unsolved. The standalone runtime works fine; full daemon
poisons slack-bolt's HTTP. `haz channel run` standalone-mode command
(spec 08 P2.5.3) would workaround it production-quality but doesn't
fix root cause.

## Things you can do right now to play

- `@hazbot hello` in any channel of bayeslearner (needs
  `/invite @hazbot` first if not already there) — with the proxy
  verified, this should hit the real agent loop.
- DM `@hazbot` directly — bot has `im:write` scope.
- Tail the log live: `tail -f /tmp/slack-standalone.log`
- Read the new commits: `git log --oneline 2ee5ccf..HEAD` (everything
  after the prior session-note refresh).

## Project structure as it stands

- **Branch**: `v2-hyperagent`. Last 3 commits this session:
  `538214b`, `58eee22`, `f56b905`. Prior session's tip was `7ee4954`.
- **Tests**: 182 channel + haz tests pass (was 171 at start; +13 new
  for spec 09 P1; +1 each for various existing-test additions).
- **Specs status**:
  - 01 SHIPPED, 02 DRAFT, 03 SHIPPED, 04 SHIPPED, 05 WITHDRAWN,
    06 SHIPPED (D7 P2.1 still open), 07 SHIPPED
  - 08 PARTIAL (D10 pivot pending — `apps.manifest.create` creates
    orphan apps; rework wizard to paste-manifest)
  - **09 PARTIAL** (P1.1–P1.8 SHIPPED; P1.9 DEFERRED, P1.10–P1.14
    deferred to follow-up)
  - 10 DRAFT (D3 corrected last session; no code yet)
  - 11 DRAFT (no code yet)
- **Slack apps in bayeslearner**: A0B5V01R4TE (the working one,
  created via UI from our manifest). Earlier orphans all deleted.
