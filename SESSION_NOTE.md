# Session note — 2026-05-23

Where things stand after the live Slack test against `bayeslearner` workspace.

## Bot is live (sort of)

A standalone Python script is holding a Socket Mode connection to Slack as
`@hazbot` in `bayeslearner`. It's an **echo bot** — sends back whatever you
@-mention it with, prefixed with "Live from hyperagent0 standalone runtime."
The agent loop isn't wired up yet; that's the next step after the framework
proof.

**Process:** `/tmp/slack-standalone.py` running under `nohup`.

**Log:** `tail -f /tmp/slack-standalone.log`

**Kill it:** `pkill -f slack-standalone`

**Restart:** `PYTHONPATH=/home/kundeng/hyperagent-zero nohup .venv/bin/python /tmp/slack-standalone.py > /tmp/slack-standalone.log 2>&1 & disown`

**Provisioned app:** A0B5V01R4TE in workspace T05FREV8RA8 (bayeslearner).
Tokens in `usr/secrets.env`. `channels.json` has `slack.enabled: true`.

## What got fixed this session

1. **Duplicate-dispatch bug**: every @-mention produced two replies because
   Slack fires both `message` and `app_mention` events for the same envelope.
   `_on_message` now skips when text contains the bot mention token.
   Regression test in `tests/test_hyperagent0_channels_slack_hardening.py`.
   Committed as part of `f7706fa`.

2. **Spec 08 design hole (D10)**: the original wizard called
   `apps.manifest.create` with the config token, but this creates **orphan
   apps** that don't show up in any user's "Your Apps" list on
   api.slack.com/apps. The user can't install them through the UI. **Apps
   created via config tokens are invisible orphans on personal/standard
   workspaces.** Documented as decision D10. Next session must rework the
   wizard to skip the create step and just hand the user the manifest JSON
   to paste into Slack's UI.

3. **README rewrite**: replaced the oversold "Wire Slack in 3 clicks" with
   an honest walkthrough that leads with Telegram (genuinely 30 seconds)
   and gives Slack the longer, accurate treatment.

## Open issues for next session

In rough order of value:

1. **P2.5.1 — pivot the Slack wizard to paste-manifest flow.** The current
   wizard creates orphans. The fix is to display the manifest JSON as a
   copy-block + paste-back instructions, instead of calling
   `apps.manifest.create`. See spec 08 D10 + P2.5.1 for details.

2. **P2.5.2 — daemon vs standalone `invalid_auth` mystery.** When the same
   SlackChannel adapter runs inside the full `haz start` daemon, Slack
   rejects `apps.connections.open` with `invalid_auth`. Standalone runtime
   works fine. Bisected individual imports + the thread+loop pattern — no
   smoking gun. Root cause is somewhere in the daemon's full runtime
   (uvicorn + init_a0 + …). Repro: `haz start -d --lan` then check
   `/home/kundeng/.hyperagent0/logs/daemon.log` for the invalid_auth
   stack. Standalone runtime is the workaround until this is solved.

3. **P2.5.3 — `haz slack run` standalone CLI.** Production-quality version
   of `/tmp/slack-standalone.py`. Boots only the channels stack (no UI,
   no LLM model loading). Useful for users who only want chat bots, AND
   bypasses issue #2.

4. **Hook the bot to the actual agent loop.** Right now it just echoes.
   The full integration is the channel→AgentContext routing that the
   `ChannelRouter` already implements — the standalone script just needs
   to use it instead of the echo handler. See
   `hyperagent0/channels/router.py`.

5. **Delete app A0B5V01R4TE** when you're done playing — it was created
   via Slack's UI for the live test. Cleaner to start fresh next time
   the wizard is reworked.

## Things you can do right now to play

- Message `@hazbot hello` in any channel of bayeslearner (after
  `/invite @hazbot` if needed). It'll echo.
- DM `@hazbot` directly — bot has `im:write` scope, will reply in the DM.
- Tail the log live: `tail -f /tmp/slack-standalone.log`

## State summary

- **Last commits**: `f7706fa` (live-test fixes), `d5ab4e5` (P2 framework),
  `9bc9871` (P1 framework). Branch `v2-hyperagent`.
- **Tests**: 157 channel + haz tests pass.
- **Memory entries saved**:
  - `feedback_follow_upstream_silently.md`
  - `project_slack_install_models.md` — the orphan-app gotcha
- **Slack apps in your workspace**: A0B5V01R4TE (the working one). All
  four config-token-created orphans were deleted.
