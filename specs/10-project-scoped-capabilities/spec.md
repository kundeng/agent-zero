---
spec_id: 10-project-scoped-capabilities
status: PARTIAL
since: 2026-05-24
until: null
epic: projects
features: [mcp-per-project, sandbox-network-per-project, skills-scope-fix, knowledge-subdir-per-project]
supersedes: []
superseded_by: null
depends_on: [09-bot-project-model]
---

# Project-Scoped Capabilities

<!-- The YAML above is the single source of truth for status and
     relationships. Never edit it outside /spec-plan. -->

## Context

Upstream agent-zero has projects (`usr/projects/<name>/.a0proj/`) with
per-project instructions, knowledge, secrets, sub-agent overrides, and
variables. Several other capabilities the agent uses are **global**
even though they have natural per-project scopes:

| Capability | Currently scoped to | Should be |
|---|---|---|
| MCP servers | Global (`settings.json.mcp_servers`) | Per-project |
| Sandbox network allowlist | Hardcoded empty in `srt.py` | Per-project (from `project.json.network`) |
| Skills | Globally shared across all projects (wildcard scan bug) | Per-project + global fallback |
| Knowledge subdir | Set at `AgentConfig` construction (global) | Already per-project via `.a0proj/knowledge/`, verify consistency |

Spec 09 makes `_default` a real project so every chat has a project to
read from. This spec is what those projects actually own.

## Constraints

- Spec 09 must land first (provides the `_default` project that makes
  "every context has a project" true).
- Conflict-surface for `python/*.py`: same as everywhere else — minimal,
  ideally just collapsing existing branches. New per-project files are
  new files, OK.
- Per-project MCP **replaces** (not overlays) global. Mental model:
  what tools does this project's agent have? You see one list.
- Per-project network allowlist **adds to** (overlays) global. Most
  workspaces want `api.anthropic.com` + `api.openai.com` globally
  reachable; projects extend with their own (e.g. their git server).

## Decisions

### D1: MCP servers per-project replace global

**Choice**: New file `usr/projects/<name>/.a0proj/mcp_servers.json`.
Same shape as `settings.json.mcp_servers`. When an `AgentContext` has
a project active:

1. Read project's `mcp_servers.json` if it exists.
2. If present and non-empty, that's the agent's MCP set — full replace.
3. If absent or empty, fall through to global `settings.json.mcp_servers`.

**Why replace, not overlay**: tools shown to the agent are part of its
identity. A project that says "you have these 3 MCP tools" should have
exactly those, not "those plus whatever global said". Otherwise the
project's intent is unclear when read in isolation.

For projects that want to inherit + extend, they can copy the global
config into their project file and add to it. Explicit > implicit.

### D2: Sandbox network allowlist per-project, layered

**Choice**: `usr/projects/<name>/.a0proj/project.json` gains an optional
`network` field:

```jsonc
{
  "title": "engineering",
  "instructions": "...",
  "network": {
    "allow": ["api.anthropic.com", "github.com", "*.example.internal"],
    "additional_allow": []  // future: dynamic additions at runtime
  }
}
```

Sandbox layering:

1. Global default: `usr/settings.json.sandbox_network_default` (list of
   hosts/patterns globally allowed for every project)
2. Project-specific: `project.json.network.allow`
3. Final allowlist = union(global, project)

`srt.py:_ensure_profile` reads both and writes the union into the per-
project profile JSON. srt's network policy uses that profile.

**Why layered**: hosts like the LLM API and a few CDNs are
universally needed; the project-specific list is for the project's
own resources. Asking every project to redeclare `api.anthropic.com`
would be annoying.

### D3: Skills are ALREADY project-scoped — confirm and document

**Re-read of `python/helpers/skills.py` after the spec draft**: the
agent-execution path is already correct. `list_skills(agent=agent)`
→ `get_skill_roots(agent)` → `subagents.get_paths(agent, "skills")`,
which resolves project → agent-profile → user → default in priority
order. The system prompt extension calls `list_skills(agent=agent)`
at `_10_system_prompt.py:88` so the agent only ever sees its active
project's skills + globals.

The wildcard `usr/projects/*/.a0proj/skills` only kicks in when
`list_skills(agent=None)` is called — and that path is only used by:

- `python/api/skills.py` — the Web UI's global skill browser
- `python/helpers/skills_cli.py` — CLI listing for the operator

Both are admin surfaces. They should show all skills across all
projects (so the operator knows what exists in the system). The
wildcard is intentional, not a bug.

**Choice**: no code change. Document that `get_skill_roots(agent=None)`
is for admin views and `get_skill_roots(agent=Agent)` is what the
agent uses.

**Why I initially called this a bug**: misread the call site for the
system prompt. The spec draft was wrong; this revision corrects it.
Live in the Log.

### D3a: `_default` project bridging for unagent'd skill resolution

**Choice (small)**: When `subagents.get_paths(agent, "skills")` falls
back to globals because no project is active, prepend
`usr/projects/_default/.a0proj/skills` once spec 09 lands its
`_default` project entity. This makes "projectless = use _default"
hold for skills too, in addition to other capabilities. ~5 lines.

**Why**: consistency with spec 09 D2. Every per-project capability
resolves through the same code path whether the project is named or
`_default`.

### D4: Knowledge subdir consistency

**Choice**: Verify (and fix if needed) that `AgentContext` resolves
its knowledge subdirs through the active project. Today
`AgentConfig.knowledge_subdirs` is set globally at construction. The
fix: when a project activates, also update the context's
`knowledge_subdirs` to include `usr/projects/<name>/.a0proj/knowledge`.

**Why**: project knowledge already exists in the schema. Making sure
the agent actually retrieves from it requires this wiring. May already
work — needs a verification pass during implementation.

### D5: UI surfaces

**Choice**: Web UI's Projects panel (`webui/components/settings/...`)
gains tabs for MCP, Network, Skills per project. Each tab edits the
right file under `.a0proj/`.

For now (P1), these are JSON editors mirroring the global MCP editor
pattern. Richer per-tool toggles can come in P2.

## Tasks

### P1 — Must Do

- [x] 1.1 `hyperagent0/projects.py` shipped 2026-05-25: `load_project_mcp_servers(name)` (raw JSON string or `None`) + `load_project_network_allow(name)` (list, defensive empty on missing/malformed). 7 tests in `tests/test_project_capabilities.py`.
- [ ] 1.2 `python/helpers/mcp_handler.py` MCPConfig resolution per-project. **Deferred** — the upstream `MCPConfig` is a process-global singleton (`get_instance()`), so per-context server sets need a non-trivial refactor (either context-aware lookup at `get_tools_prompt()` time, or separate MCPConfig instances per project). Helper `load_project_mcp_servers` is in place; the consumer needs its own session. Not blocking the Mac install since users without `mcp_servers.json` per project get the global config as today.
- [x] 1.3 `hyperagent0/sandbox/srt.py:_ensure_profile` already reads project `network.allow` + global `sandbox_network_default` and writes the union into the per-project srt profile (landed in commit 01078e0). Verified 2026-05-25; left in place pending the bigger backend-API fix to thread project NAME (not just path) through `SandboxBackend.__init__` so the `_default` `project_folder` override case resolves correctly.
- [x] 1.4 `subagents.get_paths()` falls through to `_default` for projectless agent contexts via `resolve_project_name`. Per spec 10 D3a + spec 09 P1.9 invariant. Test: `test_get_paths_falls_through_to_default_for_projectless_context`.
- [~] 1.5 Knowledge wiring verification. Read upstream code 2026-05-25: per-project knowledge IS supported via `agent.config.knowledge_subdirs` containing entries like `projects/<name>`, which `memory.abs_knowledge_dir` resolves to `usr/projects/<name>/.a0proj/knowledge`. **But it is NOT automatically wired**: `initialize.py` sets `knowledge_subdirs = [settings.agent_knowledge_subdir, "default"]` globally, and `activate_project_in_chat` doesn't mutate that list. **Operator workaround**: set `settings.agent_knowledge_subdir` to `projects/<name>` manually. **Proper fix is P2** — needs an extension or per-call resolution at `memory.py:473`.
- [x] 1.6 `Settings.sandbox_network_default` exists in `python/helpers/settings.py:139` + populated at default 2026-05-24.
- [x] 1.7 Project schema migration: covered by design. `load_project_network_allow` returns `[]` for missing-field project.json (defensive default), so existing files without `network` are equivalent to `{"allow": []}` at read time without needing a write-side migration.

### P2 — Should Do

- [ ] 2.1 Web UI Projects panel: per-project MCP editor (mirrors
  global MCP UI).
- [ ] 2.2 Web UI Projects panel: per-project network allowlist editor
  (host/pattern list, with explanation).
- [ ] 2.3 Tests: project MCP replace semantics.
- [ ] 2.4 Tests: sandbox network union layering.
- [ ] 2.5 Tests: skills only loaded from active project + globals.
- [ ] 2.6 Tests: knowledge retrieval prefers project knowledge.

### P3 — Nice to Have

- [ ] 3.1 Per-project sub-agent skill paths (override the
  `usr/agents/<profile>/skills` location).
- [ ] 3.2 Network allowlist supports CIDR ranges and ports
  (currently host patterns only).
- [ ] 3.3 MCP server dynamic add/remove via agent tool (let an agent
  request a new MCP capability mid-conversation, gated by permissions).

## Open Questions

- [ ] Do we want to support **shared MCP server sets** — e.g.
  "production projects all get the GitLab MCP + Linear MCP"? Maybe a
  named MCP profile that projects can reference.
- [ ] Should the sandbox network deny-list be supported in addition
  to allow-list? Useful for "allow * except these". Probably P3.
- [ ] Knowledge: when a project is bound to a chat at provision time,
  do we eagerly index the project's knowledge subdir? Currently
  knowledge indexing is async / lazy. Spec doesn't address; defer to
  upstream RAG behavior.

## Log

**2026-05-24** — Drafted in same conversation as spec 09. User
explicitly answered "yes scope skills to projects" and "MCP per
project too". Network allowlist was on spec 01 D8 backlog from
launch — this spec finally lands it.

**2026-05-24 (correction)** — Initial D3 claimed skills leaked
across projects via a wildcard bug. Re-reading the code showed the
agent-execution path already routes through
`subagents.get_paths(agent, "skills")` which is project-scoped. The
wildcard is only used by admin/management surfaces (Web UI skill
browser, CLI lister) which arguably should see all skills. D3
revised to document this; new D3a covers the `_default`-project
bridging once spec 09 lands. No skills code fix needed.

**2026-05-25 (P1 data-layer shipped)** — Five of seven P1 tasks
shipped, two deferred with explicit reasons:

* P1.1: `hyperagent0.projects.load_project_mcp_servers(name)` and
  `load_project_network_allow(name)` added. Both are defensive: empty
  list / `None` on missing or malformed files so the sandbox boot
  never fails on a broken project.json. 7 unit tests.
* P1.3: confirmed `srt._ensure_profile` reads project network +
  global default and writes the union (was already shipped in
  01078e0). Caveat noted: when `_default` carries a `project_folder`
  override (spec 09 P1.9), the path passed to srt is the override
  path, not the canonical `usr/projects/_default/`. The basename
  derivation in `_profile_path` and `_read_project_network_allow`
  picks the override's basename as the project name. Existing
  brokenness; properly fixed by threading project NAME through the
  `SandboxBackend` constructor — separate session.
* P1.4: `subagents.get_paths` now calls `resolve_project_name()` so
  projectless agent contexts resolve through `_default`'s
  `.a0proj/skills` (and per-profile / instructions / knowledge),
  matching the spec 09 P1.9 invariant. Test verifies it.
* P1.6: `Settings.sandbox_network_default` already shipped earlier.
* P1.7: covered by `load_project_network_allow`'s defensive default
  — old project.json files read as `{"allow": []}` without a
  write-side migration.

Deferred:

* P1.2 (per-project MCP servers): `MCPConfig` is a process-global
  singleton via `get_instance()`. Per-context server sets need
  either a context-aware lookup at `get_tools_prompt()` time, or
  separate MCPConfig instances per project. The helper
  `load_project_mcp_servers` is in place; the consumer wiring is its
  own session. Not blocking any current user since installs without
  a project `mcp_servers.json` get the global config unchanged.
* P1.5 (knowledge wiring): upstream supports per-project knowledge
  via `knowledge_subdirs` entries prefixed `projects/<name>`, but
  `activate_project_in_chat` doesn't auto-append this. Operator
  workaround: set `settings.agent_knowledge_subdir = projects/<name>`
  manually. Proper auto-wire needs an extension at memory-query
  time — moved to P2.
