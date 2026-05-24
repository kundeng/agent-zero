# Slack setup — the actual operator runbook

Last verified live 2026-05-23 against `bayeslearner` workspace (free tier).

This doc exists because Slack's developer onboarding has more sharp edges
than any other channel HyperAgent Zero supports, and we spent a long
session learning them. If you're setting up Slack for the first time —
read this first. **The wizard's "automated" path doesn't fully work for
personal/standard workspaces; you'll do some manual clicking. That's
Slack's design, not ours.**

If you only want a chat bot to play with and don't specifically need
Slack, do **[Telegram instead](#why-telegram-is-easier)** — 30 seconds
total, no admin UI dance.

## TL;DR — the steps that work

For a personal or standard-paid workspace:

1. Generate the manifest JSON (template at the bottom of this doc, or
   `haz channel provision slack --show-manifest`).
2. Go to <https://api.slack.com/apps> → green **Create New App** → **From
   a manifest** → select your workspace → paste JSON → **Create**.
3. On the new app's page, sidebar → **Install App** → **Install to
   Workspace** → **Allow**. Copy the **Bot User OAuth Token** (starts
   `xoxb-`).
4. Sidebar → **Basic Information** → scroll to **App-Level Tokens** →
   **Generate Token and Scopes** → name = anything, scope =
   `connections:write` → **Generate**. Copy the `xapp-`. Slack only
   shows it once.
5. Paste both tokens back into the wizard (or write them to secrets
   manually — see [Manual writeback](#manual-writeback) below).
6. Click Apply / restart the adapter. Bot is live.

**Skip steps 2–3 only** if your workspace is enrolled in Slack's
"next-generation platform" (paid plans + developer-program enrollment).
Then `slack app install` from the Slack CLI handles those. Most
workspaces aren't enrolled — `slack auth login` will tell you with
"This workspace is not eligible for the next generation Slack platform"
if not.

## Why it's complicated — the three Slack design choices that bite

### 1. Config tokens create *orphan* apps you can't manage

Slack has a thing called a [configuration access
token](https://api.slack.com/authentication/config-tokens) (looks like
`xoxe.xoxp-…`, ~24h lifetime). The pitch: paste it once and an
automation can call `apps.manifest.create` to register Slack apps
programmatically — no clicking around the developer console.

**The catch nobody documents prominently:** apps created this way are
not attached to any developer-user identity. They exist in the
workspace (provable via `apps.manifest.export`), but they **don't
appear in any user's "Your Apps" list at <https://api.slack.com/apps>**.
The only way to manage them is through the API. You can't click
"Install App" on them because the page isn't reachable from your UI.

We hit this hard during the live test on 2026-05-22. Created four apps,
all invisible, all undeletable through the UI. Cleaned them up with
`apps.manifest.delete` via the same config token.

**Implication:** for the install step, you need the app to be owned by
your developer identity. That means creating it interactively through
the UI's "From a manifest" wizard. The config token is useful for
*generating* the manifest JSON automatically; it's a dead end for
installation on standard workspaces.

### 2. OAuth v2 install doesn't work for non-distributable apps

The Slack manifest API returns an `oauth_authorize_url` that looks like
a normal OAuth install URL:
`https://slack.com/oauth/v2/authorize?client_id=…&scope=…`.

If you click it for an app you just created via config token, Slack
returns:

> Something went wrong when authorizing this app. Error details:
> `invalid_team_for_non_distributed_app`

Even if you append `&team=<your-team-id>`. The `/oauth/v2/authorize`
flow only accepts apps marked `is_distributable: true` (apps designed
for the Slack App Directory or enterprise-internal distribution).
Single-workspace apps (the default for `apps.manifest.create` without
extra distribution config) are not eligible.

To make OAuth v2 work, you'd need to mark the app distributable — which
requires extra manifest fields (long description, install URL, support
email, privacy policy URL) and exposes the app for public install.
That's not what you want for a personal-workspace bot.

**Implication:** the wizard's auto-capture step (a popup that clicks
Allow and posts the bot token back to a callback URL) is dead code for
non-distributable apps. The manual paste path is what actually works.

### 3. There is genuinely no API to install an app

You'd think: surely if Slack lets me CREATE an app via API, they let me
INSTALL it via API too?

They don't. There's an `apps.install` endpoint (it's real, you can hit
it) but it returns `not_allowed_token_type` for config tokens. It only
accepts admin user tokens.

You can get an admin user token through:

- **Slack CLI** (`slack login`) — but the CLI is gated behind workspace
  enrollment in Slack's "next-generation platform" (paid plans + dev
  program). Most workspaces get "This workspace is not eligible".
- **A custom Slack app with admin scopes** — chicken and egg.
- **Legacy custom integrations** — deprecated for years.

So for a regular workspace, the only path to install is **a human
clicking Install in the admin UI**. That's Slack's security boundary;
no automation gets around it.

### 4. Socket Mode tokens (`xapp-`) have no API path either

The app-level token (`xapp-…` with scope `connections:write`) is what
the bot needs to open the Socket Mode WebSocket — the channel through
which it receives messages without exposing a public HTTPS webhook.

There is **no documented API method** to mint these. They're generated
in the app's admin UI page only. The HyperAgent provisioner *tries*
`apps.connections.open` with the config token (you'd think the method
named "open" might do it); it returns `not_allowed_token_type` like
the others.

**Implication:** even if every other step were automated, this one
is always a human click + paste. Slack would have to ship a new API
to change this.

## Why Telegram is easier

For comparison, Telegram's setup is:

1. Open <https://t.me/BotFather>
2. Send `/newbot`
3. Pick a name, get a token like `123456789:ABC-DEF-…`
4. Paste it. Done.

No app entity, no scopes, no install, no separate Socket Mode token, no
admin UI. The framework supports it as a one-line provisioner. If you
just want a chat bot to interact with HyperAgent Zero, this is your
fast lane.

Discord is in the middle — paste bot token + application ID, click
the invite URL to add the bot to your server, done.

## Manual writeback (skipping the wizard)

If the UI wizard is acting up or you just have tokens from a previous
install, you can wire Slack manually:

1. Add to `usr/secrets.env`:
   ```
   SLACK_BOT_TOKEN="xoxb-…"
   SLACK_APP_TOKEN="xapp-1-…"
   ```
2. Add to `~/.hyperagent0/channels.json`:
   ```json
   {
     "slack": {
       "enabled": true,
       "token": "$$secret(SLACK_BOT_TOKEN)",
       "app_token": "$$secret(SLACK_APP_TOKEN)",
       "project_binding": {},
       "allowed_users": [],
       "allowed_chats": [],
       "require_mention": false
     }
   }
   ```
3. Restart the daemon: `haz restart`

Tokens are referenced via `$$secret(KEY)` placeholders so the JSON file
can be backed up / committed without leaking secrets. Cleartext lives
only in `usr/secrets.env`.

## Standalone runtime workaround

There's a [known issue](../specs/08-channel-provisioning-ux/spec.md#log)
where the Slack adapter fails with `invalid_auth` from
`apps.connections.open` when running inside the full `haz start`
daemon. The same adapter works perfectly as a standalone process.

Until that's resolved, you can run the Slack adapter standalone:

```bash
PYTHONPATH=/path/to/hyperagent-zero nohup .venv/bin/python \
  /path/to/slack-standalone.py > /tmp/slack-standalone.log 2>&1 &
```

A template `slack-standalone.py` lives at `/tmp/slack-standalone.py`
during active session work. A production-grade `haz slack run` command
is on the spec 08 P2.5 backlog.

## The manifest JSON

Copy-paste-ready for step 2 of the TL;DR. Replace `hazbot` with whatever
display name you want for the bot (lowercase, no spaces, 1–21 chars).
Scopes are tuned for HyperAgent Zero's Slack runtime adapter; remove
`groups:*` if you don't want private-channel access, `im:*` if you
don't want DMs.

```json
{
  "display_information": {
    "name": "HyperAgent Zero",
    "description": "HyperAgent Zero connected to Slack channels",
    "background_color": "#1a1a2e"
  },
  "features": {
    "bot_user": {
      "display_name": "hazbot",
      "always_online": true
    }
  },
  "oauth_config": {
    "scopes": {
      "bot": [
        "app_mentions:read",
        "chat:write",
        "channels:history",
        "channels:read",
        "groups:history",
        "groups:read",
        "im:history",
        "im:write"
      ]
    }
  },
  "settings": {
    "event_subscriptions": {
      "bot_events": [
        "app_mention",
        "message.channels",
        "message.groups",
        "message.im"
      ]
    },
    "socket_mode_enabled": true,
    "org_deploy_enabled": false,
    "token_rotation_enabled": false
  }
}
```

## Troubleshooting

### `invalid_team_for_non_distributed_app`
You clicked an OAuth v2 install URL for a non-distributable app. See
[design issue #2](#2-oauth-v2-install-doesnt-work-for-non-distributable-apps).
Don't use the OAuth URL — install through the app's admin UI page.

### "Page not found" on `api.slack.com/apps/<id>/install-app`
The app is an orphan (created via config token). See [design issue
#1](#1-config-tokens-create-orphan-apps-you-cant-manage). Delete the
orphan via `apps.manifest.delete` and recreate via UI from manifest.

### `invalid_auth` from `apps.connections.open`
**If running standalone**: the `xapp-` token is wrong, missing the
`connections:write` scope, or belongs to a different app than the
`xoxb-`. Regenerate it for the correct app.

**If running inside the full daemon**: known issue — see the
[Standalone runtime workaround](#standalone-runtime-workaround). The
same tokens work standalone; something in the daemon's runtime
poisons slack-bolt's HTTP path.

### "This workspace is not eligible for the next generation Slack platform"
Your workspace isn't enrolled in Slack's developer program. You can't
use the Slack CLI's install command. Fall back to the manual UI install
flow (steps 2–3 of the TL;DR).

### Bot replies twice to every @-mention
Fixed in `f7706fa` (2026-05-23). Slack fires both `message` and
`app_mention` events for a single @-mention; the adapter now skips the
`message` handler when the text contains the bot mention token.
Regression test in `tests/test_hyperagent0_channels_slack_hardening.py`.

### Bot doesn't respond at all
- Verify Socket Mode is open: `tail -f /tmp/slack-standalone.log`,
  look for `⚡️ Bolt app is running!`
- Verify the bot is in the channel you @-mention from. Slack bots
  only receive `app_mention` events in channels they've been invited
  to. Type `/invite @hazbot` in the channel.
- DMs always work as long as the bot has `im:write` + `im:history`
  scopes — try DM'ing the bot directly first as a connectivity test.

## How we figured this out

See `specs/08-channel-provisioning-ux/spec.md` decision D10 and the
2026-05-23 Log entry for the live-test narrative. Two memory entries
also capture the lessons:

- `~/.claude/projects/-home-kundeng-hyperagent-zero/memory/project_slack_install_models.md`
- `~/.claude/projects/-home-kundeng-hyperagent-zero/memory/feedback_follow_upstream_silently.md`

## See also

- [Spec 08 — Channel Provisioning UX](../../specs/08-channel-provisioning-ux/spec.md) — design decisions, especially D10
- [Slack official manifest spec](https://api.slack.com/reference/manifests)
- [Slack OAuth v2 docs](https://api.slack.com/authentication/oauth-v2)
- [Slack config tokens](https://api.slack.com/authentication/config-tokens)
- [Slack CLI](https://docs.slack.dev/tools/slack-cli/) — note the
  workspace eligibility gate
