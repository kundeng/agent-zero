# How HyperAgent Zero actually works

> Vacation reading. The README tells you the commands to run; this doc
> tells you *what those commands do*, where state lives, and how the
> pieces fit. It's written narrative-style — start at the top, follow
> the links if you want to go deeper. Last refreshed 2026-05-26.

## What this fork is (in one paragraph)

HyperAgent Zero is a fork of [agent0ai/agent-zero](https://github.com/agent0ai/agent-zero)
that flips the runtime model from **container-first** ("agent runs inside
Docker, your host runs Docker") to **host-first** ("agent runs on your
host or VM, and Docker is one of three optional code-execution
sandboxes"). Upstream's `python/`, `agent.py`, `models.py`, `prompts/`,
`webui/`, etc. are kept untouched and cherry-pickable; everything we add
sits next to them under `hyperagent0/` plus a `haz` CLI. The point is to
let an agent live next to your code (and chat channels, and project
folders) without the Docker-Desktop ceremony, while still being able to
opt back into containerized code execution per-install.

If you want the design rationale rather than the user-visible behavior,
the source of truth is `specs/01-host-first/spec.md` plus the spec map
at the bottom of this doc.

---

## Three ways to run it, side by side

There are three supported install paths. They all end at the same web
UI on the same port (`http://localhost:50080`), but what's actually
running on your machine is genuinely different in each case. This is
the table the README implies but doesn't spell out:

| | **Docker run** | **Host install** | **Compose** |
|---|---|---|---|
| Command (TL;DR) | `docker run ... bayeslearner/hyperagent0` | `curl ... \| bash` | `docker compose up -d` |
| Where the agent process runs | inside one container | on your host, as a normal Python process | inside one container |
| Where Python lives | `/usr/local/bin/python` in the container | `~/.hyperagent0/venv/bin/python` on your host | container, same as Docker run |
| Where the source code lives | baked into the image at `/app/` | `~/.hyperagent0/repo/` on your host | baked into the image |
| Where state lives | named volumes (`hyperagent0`, `hyperagent0_memory`, ...) | `~/.hyperagent0/` + `usr/` inside the repo checkout | named volumes |
| Code-execution sandbox runs... | inside the same container as the agent (no extra isolation) | wherever you point `sandbox_mode` (host / srt / ssh) | inside the container |
| Updates | `docker pull` and re-run | `git -C ~/.hyperagent0/repo pull` (or rerun the curl one-liner) | `docker compose pull && up -d` |
| When to pick this | "I just want to try it" | "I want the agent to *see my host* — edit my repos, run my dev server, talk to my LAN" | "I want a long-running install that survives reboots" |

The host install is the path the fork was built for: the daemon runs
where your files are. The two Docker paths exist mostly for "I don't
want to install Python on this box" and "I'm putting this on a VPS
that should survive reboots."

A subtlety: in the two Docker paths, **the code-execution sandbox is
inside the same container as the agent**. There's no second isolation
boundary — the agent and the code it runs share a container. If you
want real isolation in the Docker paths, that's a future spec; today
the sandbox-mode toggle only does meaningful work in the host install.

---

## What `curl ... | bash` actually does

The host install (`install.sh`) is the most magical-looking step, so
it's worth unpacking. Here's what happens when you run:

```bash
curl -fsSL https://raw.githubusercontent.com/kundeng/hyperagent-zero/v2-hyperagent/install.sh | bash
```

`install.sh` is 263 lines and runs five phases (see `install.sh`):

**Preflight** — checks `git` is on PATH and finds a Python ≥ 3.12.
Upstream `agent.py` uses PEP 695 syntax (the `type X = ...` shorthand),
so 3.11 won't parse it. The script tries `python3.12`, `python3.13`,
`python3` in that order and bails with platform-specific install
instructions if none works.

**Phase 1 — git clone.** Clones the `v2-hyperagent` branch into
`~/.hyperagent0/repo/` (or pulls if the directory already exists).
**The whole repo lands on your disk.** This is intentional: see
"why we clone the whole repo" below. Skipped in `--dev` mode, which
uses your current directory as `REPO_DIR`.

**Phase 2 — venv.** Creates a fresh Python venv at
`~/.hyperagent0/venv/`. Reused on re-runs.

**Phase 3 — CPU torch.** Installs `torch==2.4.0` from PyTorch's CPU
index. This is the big download (~200 MB). It runs first because it
needs a separate `--index-url`; mixing it with the main resolve causes
pip to either explode or pull CUDA wheels you don't want.

**Phase 4 — `requirements.txt` + `hyperagent0[all]`.** Installs
upstream agent-zero's pinned deps, then installs our package editable
(`pip install -e .[all]`) against `~/.hyperagent0/repo/`. **Editable
means there's no `hyperagent0` copy in `site-packages/` — the venv
points back at the cloned repo.** This is why `git pull` is enough to
upgrade: your `haz` binary already points into the repo, so source
changes appear immediately.

**Phase 5 — `requirements2.txt` (pin overrides).** Runs last so its
pins win over whatever Phase 4's transitive resolution chose. Today
this is mostly forcing `openai==1.99.5` after `browser-use` would
downgrade it to `1.99.2`.

**Site bootstrap.** Drops a tiny `.pth` file into the venv's
`site-packages/` that adds `~/.hyperagent0/repo/` to `sys.path` and
sets `HYPERAGENT0_REPO` as an environment variable on every Python
invocation in this venv. This is what makes `import agent` work even
though `agent.py` isn't a proper package.

**Symlinks.** Creates `~/.local/bin/haz` and `~/.local/bin/hyperagent0`
pointing at the venv's entry points. If `~/.local/bin` isn't on your
`PATH`, the installer prints a warning.

### Why we clone the whole repo (instead of just `pip install`-ing a wheel)

Upstream agent-zero isn't a proper Python package. It expects to run
from a project root with `agent.py`, `models.py`, `prompts/`,
`webui/`, etc. all reachable as top-level paths. The pip wheel we
publish only ships the `hyperagent0/` and `python/` directories — the
upstream assets aren't there, and we don't want to copy them in because
that would make every wheel build a giant blob and prevent the
"cherry-pick from upstream" workflow.

So the wheel alone is insufficient. The repo on disk is the
runtime — `install.sh` clones it, and `hyperagent0.paths.repo_root()`
(in `hyperagent0/paths.py`) is the resolver that finds it again at
import time. The resolution order is:

1. `$HYPERAGENT0_REPO` (set by the `.pth` file at venv activation).
2. Walking up from the `hyperagent0` package's `__file__` (catches
   editable installs).
3. `~/.hyperagent0/repo/` as a final fallback.

The trade-off — vs the alternative of bundling everything into the
wheel — is that you carry a git checkout around forever, and that
checkout is the upgrade unit. Once you've decided to live with that,
the upgrade story gets very nice: `git pull` is the entire update, no
reinstall, no `pip` involved.

---

## What's running once `haz start` is up

After `haz start -d`, the process tree is:

```
(your shell, returned)
│
└── python -m run_ui (PID in ~/.hyperagent0/hyperagent0.pid)
    ├── uvicorn worker — serves the Flask app + Socket.IO over HTTP
    ├── agent monologue loops (one per active chat context)
    ├── channel adapters (Slack / Telegram / Discord — only if configured)
    └── code-execution sessions (spawned per tool call)
```

It's one Python process. There's no separate worker pool, no message
queue, no Redis. The Flask + Socket.IO stack runs under uvicorn (see
`run_ui.py`). Per-chat agent loops are async tasks inside that
process; channel adapters are also async tasks; the code-execution
sandbox is a child process the agent spawns via `subprocess` /
`asyncssh` / `srt`, depending on `sandbox_mode`.

### Where state lives

Two roots:

**`~/.hyperagent0/`** — daemon-owned state. Doesn't belong to any
single chat or project.

```
~/.hyperagent0/
├── repo/                 # the git checkout (curl install path only)
├── venv/                 # the python venv (curl install path only)
├── hyperagent0.pid       # daemon PID file — written by daemon.py
├── hyperagent0.lock      # flock-based singleton guard
├── daemon.sock           # ctl socket (reserved; unused today)
├── logs/daemon.log       # stdout/stderr capture
├── channels.db           # SQLite for channel thread state (spec 06)
├── channels.json         # operator-edited channel config (spec 08)
└── sandbox/<project>.json  # srt profile written by srt.py per project
```

**`<repo>/usr/`** — agent-owned content. Chat history, settings, your
projects, vector memory, knowledge. This is the directory you'd back
up and migrate.

```
usr/
├── settings.json              # the operator's settings (LLM, sandbox_mode, ...)
├── chats/<context_id>.json    # one file per chat context
├── projects/<name>/.a0proj/   # per-project metadata
│   ├── project.json           # title, description, instructions, network.allow, ...
│   ├── mcp_servers.json       # per-project MCP override (spec 10 D1)
│   ├── instructions/          # extra .md prompts injected into system prompt
│   ├── knowledge/             # per-project RAG knowledge
│   └── skills/                # per-project SKILL.md files
├── projects/_default/         # the implicit project (spec 09 D2) — always exists
├── memory/                    # global vector memory (FAISS)
└── secrets.env                # global secrets (also $$secret(...) in channels.json)
```

A clean migration is `tar czf hyperagent0-backup.tgz ~/.hyperagent0/{channels.db,channels.json} <repo>/usr/`. Everything else — venv, repo, logs — is
regeneratable.

### Cold start budget

`haz status`, `haz stop`, `haz --help`, `haz check --help` all have a
hard **< 200 ms** cold-start budget (spec 03 D5). The way this is
enforced: nothing in `hyperagent0/cli.py` or any subcommand's *import*
path is allowed to import LiteLLM, Flask, channel SDKs, etc. Heavy
imports go *inside* the command body, so `haz status` doesn't pay for
machinery it never touches. The CI smoke test asserts `haz status` runs
in under 1 s on a fresh runner.

---

## The three sandbox modes — what `none`, `srt`, `ssh` actually mean

`sandbox_mode` is a single global setting in `usr/settings.json`. The
agent's `code_execution_tool` resolves it through a registry
(`hyperagent0/sandbox/__init__.py`) and dispatches to one of three
backends. Spec 05 (project-level sandbox override) was withdrawn
2026-05-22 — there is no per-project sandbox mode.

**`none`** (`hyperagent0/sandbox/none.py`) — bare PTY subprocess on the
host (or in the container, if you're running Docker). The agent's
shell IS your shell. `cd`, `export`, `source`, sudo, `apt install`,
`docker run`, `rm -rf` — all available with whatever privileges the
daemon has. Use this when the agent is *trusted* and you want it to
operate on your files and dev environment with no friction. This is
the host-install default.

**`sandbox`** (`hyperagent0/sandbox/srt.py`) — wraps the bash session
in Anthropic's [sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime)
(`srt`, an npm package: `@anthropic-ai/sandbox-runtime`). The
distinction worth understanding: `srt` wraps the *whole bash*, not each
command. Earlier drafts of this backend re-spawned `srt` per command,
which killed shell state (`cd`, `export`, etc.) between commands.
The current design wraps the long-lived bash so `set -x; cd /tmp;
export FOO=bar` survives across the agent's commands, while srt's
syscall filter + network policy stay in effect. The profile is written
to `~/.hyperagent0/sandbox/<project>.json` on every shell open; the
network allowlist is the union of `Settings.sandbox_network_default`
and the project's `network.allow` (spec 10 D2).

**`ssh`** (`hyperagent0/sandbox/ssh.py`) — opens an `asyncssh` PTY to
a remote host you control. The agent runs code on that host instead of
locally. Use this when you have a dev VM or container you want the
agent to operate inside, and you don't want it to touch your laptop.

If your `settings.json` still has the legacy `ssh_enabled=true` from
pre-spec-05 days, it auto-migrates to `sandbox_mode=ssh` with a one-time
deprecation warning.

The single-global rule is the result of spec 05's withdrawal — there is
no "per-project sandbox" today. Per-project network allowlist (which
*is* shipped) lives at a different layer and is unioned into whatever
sandbox mode is global.

---

## Projects, and the `_default` collapse

Upstream agent-zero has projects: `usr/projects/<name>/.a0proj/` with
per-project instructions, knowledge, skills, sub-agent overrides,
secrets. Originally these were *optional* — a chat could be "projectless,"
and a lot of code had `if project_name:` branches to handle that case.

Spec 09 D2 collapsed that. Every chat now lives in *some* project; if
the operator never explicitly assigns one, it's bound to a special
project named `_default` that the daemon auto-creates on first start
(see `hyperagent0/projects.py:ensure_default_project`). The
`if project_name:` branches across the codebase were replaced with
`resolve_project_name(name)` calls that return `_default` for `None`.

Why this matters for understanding the code: when you see something
like `agent.context.get_data("project")` returning `None`, that does
NOT mean "no project" — it means "use `_default`." The `_default`
project can be edited like any other (it has a `project.json` with
instructions, can have its own `mcp_servers.json`, etc.); it's special
only in that it's auto-created.

Spec 10 then made several capabilities per-project that were previously
global:

* **MCP servers** — `usr/projects/<name>/.a0proj/mcp_servers.json`
  *replaces* the global `settings.json.mcp_servers` for that project
  (D1, replace semantics). The resolver
  (`hyperagent0/mcp.py:get_mcp_config_for_agent`) caches one
  `MCPConfig` instance per project name and falls through to the
  global singleton when no per-project file exists.
* **Sandbox network allowlist** — `project.json.network.allow` is
  *unioned* with `Settings.sandbox_network_default` (D2, overlay
  semantics) into the srt profile.
* **Skills** — already per-project upstream; spec 10 D3a added the
  `_default` fallthrough so projectless contexts still see
  `_default`'s skills.
* **Knowledge** — partially wired (writers exist, auto-injection into
  `agent_knowledge_subdir` is a P2 task; operator workaround is to set
  `agent_knowledge_subdir=projects/<name>` manually).

The asymmetry (MCP replaces, network unions) is deliberate. MCP tools
are part of the agent's identity ("you have these tools"); explicit is
better than additive. Network hosts are infrastructure ("the LLM API
plus your project's git server"); requiring every project to redeclare
`api.anthropic.com` would be annoying.

---

## A Slack message becomes an agent turn

The channels system (specs 04, 06, 08, 09) is the most moving-parts
subsystem in the fork. Here's the path a single user message takes,
from "user types in Slack" to "agent replies":

1. **Slack delivers the event.** The agent's Slack adapter
   (`hyperagent0/channels/slack.py`) holds a long-lived
   Socket-Mode connection to Slack (no public webhook URL needed —
   the adapter dials *out*). The event arrives as a JSON blob.

2. **Adapter normalizes to a `ChannelMessage`.** `slack.py` decodes
   the event into a platform-agnostic `ChannelMessage` (see
   `hyperagent0/channels/base.py`), filtering out bot messages, edits,
   and channels the bot isn't a member of. Mention-awareness lives
   here (spec 06): if the message is in a channel rather than a DM,
   the bot only responds if mentioned (`@bot`).

3. **Router decides the project + chat context.**
   `hyperagent0/channels/router.py` looks up the `(platform, bot_name,
   channel_id, thread_id)` tuple in `~/.hyperagent0/channels.db` (the
   `ThreadStore`). If a context already exists for that thread, it's
   reused; otherwise a new chat context is created. The bot's
   configured project (from `channels.json`) is activated on that
   context.

4. **Agent loop runs.** The router pushes the message into the
   normal `Agent.monologue()` loop (the same one the web UI drives).
   The agent generates a response, possibly using MCP tools (resolved
   through the per-project `MCPConfig` if one exists), possibly
   invoking the code-execution sandbox.

5. **Reply goes back.** The adapter's `send_message` posts the
   response in-thread (spec 06 reply-to behavior). For long
   responses, the formatter (`hyperagent0/channels/formatter.py`) does
   platform-specific markdown handling.

The channel adapters are configured via the Settings UI's Channels tab
(spec 08) or via `haz channel provision <platform>`. Generated tokens
are written to `usr/secrets.env` with `$$secret(...)` placeholders in
`~/.hyperagent0/channels.json` — same convention upstream uses for
other secrets.

**Multi-bot per platform** is supported (spec 09): you can have two
Slack apps installed into the same workspace, each bound to a
different project. The `ThreadStore` keys on `bot_name` to keep the
chat contexts separate.

---

## Where this fork ends and upstream begins

A useful mental model for reading the code:

* **Upstream files** (`agent.py`, `models.py`, `run_ui.py`,
  `python/*`) — left untouched whenever possible. Modifications happen
  only at the **conflict-surface budgeted** sites called out in each
  spec (e.g., `models.py` gains the `claude-sdk` provider dispatch in
  spec 02; `agent.py:876` routes MCP tool lookup through our resolver
  in spec 10). Every patch site is documented in the spec it came
  from. The rest is upstream-pristine and cherry-pickable.

* **Our additions** all live under `hyperagent0/` (the package),
  `docker/hyperagent0/` (the container image), `install.sh`, and
  `specs/` (design docs). New behavior should go in `hyperagent0/`
  even if it integrates with an upstream system — the pattern is
  "sibling resolver wraps the upstream singleton" (see
  `hyperagent0/mcp.py` for the canonical example).

* **Extensions over core edits.** Upstream's extension system
  (`python/helpers/extension.py`'s `@extensible` decorator) lets us
  hook into the agent loop at named points (`before_main_llm_call`,
  `system_prompt`, `monologue_start`, etc.) without modifying
  `agent.py`. Anything that *can* be an extension, *should* be — even
  if it means a slightly more roundabout implementation.

When you see something that breaks these rules, it's either (a) the
budgeted patch site for a spec, or (b) tech debt awaiting cleanup.

---

## Reading guide for the specs

There are 11 spec folders under `specs/`. They're numbered by
inception order, but a more useful read order groups them by topic.
The `specs/README.md` is the authoritative status board; this is a
narrative version. (Note: the per-spec frontmatter sometimes says
`DRAFT` when the spec is actually shipped — `CLAUDE.md`'s "What
we're building" table is the source of truth.)

**The platform shape** — read these to understand why the fork exists.
* **01 host-first** — the foundational pivot from container-first to
  host-first. Why it's the way it is. SHIPPED.
* **05 project-isolation** — explicitly WITHDRAWN. Worth reading the
  withdrawal rationale; it documents what we *chose not to build*.

**The install + lifecycle layer.**
* **03 daemon-cli** — `haz` CLI design, cold-start budget,
  systemd integration. SHIPPED.
* **07 install-ux** — what `install.sh` does, why the repo lands on
  disk, the wheel-vs-repo design. SHIPPED.

**The LLM provider layer.**
* **02 claude-sdk** — Claude Pro/Max subscription path via the local
  `claude` CLI subprocess. SHIPPED. Worth reading for the D1 reframe
  log entry, which explains why we don't use the Anthropic API key.

**The chat-channel stack.** Read in order — each builds on the prior.
* **04 chat-channels** — adapter interface + Telegram, Slack,
  Discord implementations. SHIPPED.
* **06 channel-hardening** — mention-awareness, reply-to, SQLite
  migrations. SHIPPED.
* **08 channel-provisioning-ux** — Settings → Channels UI + the
  `haz channel` CLI + the (painful) Slack manifest flow. SHIPPED.
  See also `docs-haz/channels/slack-setup.md` for the operator runbook
  that captures the live-tested install path.
* **09 bot-project-model** — multi-bot per platform, `_default`
  project, ThreadStore keying by bot_name. SHIPPED. The
  `_default` collapse described above lives here.

**The per-project capability layer.**
* **10 project-scoped-capabilities** — per-project MCP servers (D1,
  replace), per-project network allowlist (D2, union), skills + knowledge
  scoping. P1 SHIPPED 2026-05-26, P2 backend SHIPPED, P2 frontend
  pending.

**Outstanding work.**
* **11 tool-permissions** — DRAFT. Permission model for the agent's
  built-in tools, ask-via-channel approvals, native read/edit/write
  tools.

If you're trying to find "where was this decided?", the spec's `## Log`
section at the bottom is the per-task history. The spec's frontmatter
(`status`, `since`, `until`) is the index. CLAUDE.md's "What we're
building" table is the cross-spec summary maintained by hand.

---

## Things this doc deliberately doesn't cover

* **The agent's own behavior** — how `monologue()` decides what to do,
  how tools are dispatched, how memory retrieval works. That's
  upstream's responsibility; `python/helpers/extension.py` and
  `python/tools/` are the entry points if you want to dig in.
* **Web UI internals** — Vue components in `webui/` are upstream code.
  Spec 10 P2.1/P2.2 will add per-project MCP + network editor tabs,
  but the underlying patterns are upstream's.
* **Per-tool documentation** — each tool's behavior lives in its file
  under `python/tools/` with prompt files in `prompts/default/`.

The README has the "do this" commands, `CLAUDE.md` has the
machine-readable conventions, the specs have the design decisions,
and this doc is the narrative bridge between them. If you find an
honest-to-god *gap* between what this doc says and what the code
does — file an issue. Stale docs are worse than no docs.
