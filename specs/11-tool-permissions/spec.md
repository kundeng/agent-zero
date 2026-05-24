---
spec_id: 11-tool-permissions
status: DRAFT
since: 2026-05-24
until: null
epic: tools
features: [permission-model, allow-deny-patterns, ask-via-channel, native-read-tool, native-edit-tool, native-write-tool, native-glob-tool, native-grep-tool, native-webfetch-tool]
supersedes: []
superseded_by: null
depends_on: [09-bot-project-model, 10-project-scoped-capabilities]
---

# Tool Permissions + Claude Code Tool Parity

<!-- The YAML above is the single source of truth for status and
     relationships. Never edit it outside /spec-plan. -->

## Context

Upstream agent-zero's tools run with no consent gate. The
`code_execution_tool` will happily `rm -rf /` if asked (modulo sandbox
mode restrictions). The harness has no "ask before running this" model
at the tool layer, and no allow/deny pattern matching. Spec 06 D7
added an `on_action` callback for ask-question cards but no tool uses
it for permission gating.

For Slack/Telegram-driven agents, this is a real safety gap: a user
in the workspace can ask the bot to do something destructive and the
bot just does it.

**Claude Code's permission model** is the proven shape:

```jsonc
{
  "permissions": {
    "default_mode": "ask",
    "allow": ["Read(*)", "Bash(git status*)", "Grep(*)"],
    "deny":  ["Bash(rm -rf*)", "Write(/etc/*)"]
  }
}
```

This spec adopts that shape, wires it into agent-zero's tool execution
chain, and adds new native tools so agent-zero has rough parity with
Claude Code's tool surface (currently agent-zero relies heavily on
shelling out via `code_execution_tool` for what should be structured
operations).

## Constraints

- Permission lookup must be fast (per-tool-call, can be many per
  agent turn). Cache compiled patterns.
- Backward compat: existing agent-zero installs without
  `permissions.json` default to `mode=auto` (current behavior — run
  everything). New installs default to `mode=ask`.
- For channel-driven chats, "ask" round-trips via the spec 06 D7
  `on_action` callback that already exists in the router. For web UI,
  reuse the existing ask-question card flow.
- New native tools shouldn't break existing agent prompts that rely
  on `code_execution_tool` for file ops. Tools coexist; new ones are
  additional, not replacements.

## Decisions

### D1: Permission file shape + lookup precedence

**Choice**: `.a0proj/permissions.json` per project, `usr/permissions.json`
global. On each tool invocation:

```python
def resolve_decision(tool_name: str, args: dict, project: str) -> str:
    project_perm = load_project_permissions(project)
    global_perm  = load_global_permissions()
    invocation   = f"{tool_name}({format_args(args)})"

    # explicit project deny wins over everything
    if any(match(p, invocation) for p in project_perm.deny): return "deny"
    if any(match(p, invocation) for p in global_perm.deny):  return "deny"
    if any(match(p, invocation) for p in project_perm.allow): return "allow"
    if any(match(p, invocation) for p in global_perm.allow):  return "allow"
    return project_perm.default_mode or global_perm.default_mode or "ask"
```

Returns one of: `allow` (run silently), `deny` (refuse with explanation
to agent), `ask` (round-trip to user), `auto` (run, log it), `bypass`
(run, don't log).

**Why**: minimal precedence rules, easy to reason about. Project deny
> project allow > global deny > global allow > default mode. Matches
how Claude Code resolves.

### D2: Pattern matching shape

**Choice**: Glob-style patterns over the canonical invocation string:

- `Read(*)` — any Read call
- `Bash(git status*)` — Bash with command starting `git status`
- `Bash(* | grep *)` — pipes containing grep
- `Write(/etc/*)` — Write to anything under /etc
- `WebFetch(https://github.com/*)` — WebFetch to github URLs

Pattern compilation: `fnmatch.translate` to regex, cached per-pattern.
For tools with structured args (Write(path, content)), `format_args`
serializes to `Write(<path>)` — content is not part of the matcher
(too volatile, and we don't want to leak secrets through pattern logs).

**Why**: matches Claude Code's pattern shape exactly. Users who know
one know both.

### D3: Ask mode round-trip via channel

**Choice**: For channel-driven contexts, "ask" mode:

1. Tool execution pauses.
2. Adapter posts a card (Slack blocks / Telegram inline keyboard /
   Discord buttons) with the pending invocation and `Allow / Allow always
   / Deny / Deny always` buttons.
3. User clicks. Adapter's `on_action` callback fires with the choice.
4. Router resolves the pending invocation: `Allow once` → run + don't
   persist. `Allow always` → run + append to project allow list.
   `Deny` → refuse with explanation. `Deny always` → refuse + append
   to project deny list.

For web-UI-driven contexts: reuse the existing ask-question card flow.

If the user is offline / unresponsive for >5 min, default to "deny once"
and tell the agent so it can route around.

**Why**: same surface across all chat platforms, same surface across
chat and web UI. The Allow-always / Deny-always shortcut is the
killer feature — gives users a "trust this kind of action" without
hand-editing permissions.json.

### D4: New native tools for Claude Code parity

**Choice**: Six new tools in `python/tools/`:

| Tool | Signature | Replaces (in bash) |
|---|---|---|
| `read_file` | `read_file(path, offset=None, limit=None) → text` | `cat`, `head`, `tail`, `sed -n` |
| `write_file` | `write_file(path, content) → ok` | `echo > file`, `cat <<EOF > file` |
| `edit_file` | `edit_file(path, old, new, replace_all=False) → diff` | `sed -i s/x/y/g` (with safety: requires exact match) |
| `glob_files` | `glob_files(pattern, base=cwd) → [paths]` | `find . -name X` |
| `grep_files` | `grep_files(pattern, path=cwd, regex=False) → [matches]` | `grep -r` |
| `web_fetch` | `web_fetch(url) → markdown` | `curl` + html-to-md |

All six respect the permission model. All six emit structured output
the agent can parse (no shell parsing required). They use the same
internal `files.read_file` / `files.write_file` helpers upstream
already has — wrappers over those with stricter types and the
permission gate.

Tools are registered in `python/tools/__init__.py` via the existing
tool-discovery mechanism (drop a file in `python/tools/`, it shows up).

**Why parity**: agents trained on Claude Code (which is most useful
agents these days) expect Read/Edit/Write/Glob/Grep to exist as
primary file operations. Forcing them to shell out via bash for these
is friction. Also lets the permission model express things like
"allow Read(*) but deny Write(/etc/*)" without having to grep through
arbitrary bash strings.

### D5: Default-mode bootstrap

**Choice**: Fresh installs get a sane starter permissions file:

```jsonc
// usr/permissions.json (created on first daemon start if missing)
{
  "default_mode": "ask",
  "allow": [
    "Read(*)",
    "Glob(*)",
    "Grep(*)",
    "Bash(ls*)",
    "Bash(pwd*)",
    "Bash(git status*)",
    "Bash(git log*)",
    "Bash(git diff*)"
  ],
  "deny": [
    "Bash(rm -rf*)",
    "Bash(:* :|:&:*)",
    "Bash(*sudo*)",
    "Bash(*chmod 777*)",
    "Bash(curl * | sh*)",
    "Bash(wget * | sh*)",
    "Write(/etc/*)",
    "Write(/var/*)",
    "Write(/usr/*)",
    "Write(/root/*)",
    "Edit(/etc/*)",
    "Edit(/var/*)",
    "Edit(/usr/*)",
    "WebFetch(file:*)"
  ]
}
```

Read-only stuff is silently allowed. Common safe bash is silently
allowed. Obvious-destructive bash is silently denied. Everything else
asks.

**Why**: a useful default. The user can extend or relax via the UI.

## Tasks

### P1 — Must Do

- [ ] 1.1 `hyperagent0/permissions.py` (new module): file loaders for
  global + per-project permissions, pattern compilation cache,
  `resolve_decision(tool_name, args, project) → mode`.
- [ ] 1.2 Hook point: extend `python/helpers/extension.py` to fire a
  `before_tool_execute` extension that consults permissions. Existing
  `tool_execute_before` extension folder is the right place; add
  `_50_permissions.py`.
- [ ] 1.3 Ask round-trip for channel contexts: extend the
  spec 06 D7 `on_action` plumbing. Adapter card-render is per-platform
  (Slack blocks / Telegram inline keyboards / Discord components).
  Slack adapter ships first; Telegram / Discord follow in P2.
- [ ] 1.4 Ask round-trip for web UI: hook into the existing
  ask-question card flow.
- [ ] 1.5 Persistence: "Allow always" / "Deny always" appends to
  `.a0proj/permissions.json`.
- [ ] 1.6 New tool `python/tools/read_file.py` (Read).
- [ ] 1.7 New tool `python/tools/write_file.py` (Write).
- [ ] 1.8 New tool `python/tools/edit_file.py` (Edit).
- [ ] 1.9 New tool `python/tools/glob_files.py` (Glob).
- [ ] 1.10 New tool `python/tools/grep_files.py` (Grep).
- [ ] 1.11 New tool `python/tools/web_fetch.py` (WebFetch). Reuses
  `python/helpers/browser_use_monkeypatch` for content extraction
  where possible.
- [ ] 1.12 Default-permissions bootstrap on first daemon start
  (creates `usr/permissions.json` with D5 starter content if absent).

### P2 — Should Do

- [ ] 2.1 Web UI permissions editor (Settings → Permissions tab).
- [ ] 2.2 Slack adapter renders ask-cards with block-kit buttons.
- [ ] 2.3 Telegram adapter renders ask-cards with InlineKeyboardMarkup.
- [ ] 2.4 Discord adapter renders ask-cards with components.
- [ ] 2.5 Tests: pattern matching (allow precedence, deny precedence,
  fall-through to default mode).
- [ ] 2.6 Tests: each new tool roundtrips with both `allow` and `ask`
  decisions.
- [ ] 2.7 Tests: ask timeout falls back to deny.
- [ ] 2.8 Tests: "Allow always" persists to project permissions file.
- [ ] 2.9 Docs: `docs/permissions.md` — operator guide for editing the
  permissions file, pattern shape, examples.

### P3 — Nice to Have

- [ ] 3.1 Bash command parsing (shell AST) so `Bash(rm -rf /)` can be
  matched even when written as `Bash(    rm -rf  / )` with weird
  whitespace. Currently glob over the raw string.
- [ ] 3.2 Rate limits per pattern (e.g., `WebFetch(*)` capped at 30
  calls/min globally).
- [ ] 3.3 Audit log of all tool invocations + decisions to
  `~/.hyperagent0/logs/permissions-audit.log`.
- [ ] 3.4 Permission inheritance from a "permission set" (named
  policy that projects can reference). Mirrors the MCP-profile idea
  from spec 10 OQ.
- [ ] 3.5 Permission diff UI: "what would change if I switched mode
  from ask to auto for this project?"

## Open Questions

- [ ] When the agent is on a sub-agent call (`call_subordinate`), do
  permissions apply to the sub-agent's tool calls too? Probably yes —
  sub-agent inherits parent's permission context. Verify during impl.
- [ ] How does this interact with sandbox mode? Sandbox restricts what
  the OS allows; permissions restrict what the harness allows. They
  layer: harness allows → sandbox may still deny → command fails. We
  don't try to merge them.
- [ ] For `WebFetch`, the deny pattern format for hosts —
  `WebFetch(http*://example.com/*)` is awkward. Consider a dedicated
  `hosts.allow` / `hosts.deny` config that WebFetch and any sandbox
  network policy both read.

## Log

**2026-05-24** — Drafted in response to user request for
Claude-Code-style allow/deny/ask permission model + parity with
Claude Code's tool surface. Agent-zero's current `code_execution_tool`
covers Bash but the harness lacks Read/Edit/Write/Glob/Grep/WebFetch
as first-class tools, forcing agents to shell out for routine file
ops. This spec lands both halves: the permission model and the tool
parity, because they share the same hook point in the tool execution
chain.
