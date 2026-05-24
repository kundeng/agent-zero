---
spec_id: 10-project-scoped-capabilities
status: DRAFT
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

### D3: Skills filtered to active project + global fallback

**Choice**: Fix the bug in `python/helpers/skills.py:list_skills()`.
Replace the wildcard `usr/projects/*/.a0proj/skills` with explicit
project resolution:

```python
def list_skills(agent):
    project = projects.get_context_project_name(agent.context) or "_default"
    paths = [
        f"usr/projects/{project}/.a0proj/agents/{agent_profile}/skills",
        f"usr/projects/{project}/.a0proj/skills",
        f"usr/agents/{agent_profile}/skills",
        f"agents/{agent_profile}/skills",
        "usr/skills",      # NEW: global user skills
        "skills",          # NEW: builtin global skills
    ]
    return _load_from_paths(paths)
```

**Why**: the user explicitly stated skills are per-project (rationale:
a skill named "deploy" means different things in different projects).
Global skills (`usr/skills/` and `skills/`) still exist as a shared
library for genuinely-cross-project things (e.g. a "search the web"
skill).

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

- [ ] 1.1 `hyperagent0/projects.py` (new file from spec 09) gains
  `load_project_mcp_servers(name) -> str | None` and
  `load_project_network_allow(name) -> list[str]`.
- [ ] 1.2 `python/helpers/mcp_handler.py` MCPConfig resolution
  detects active project (from current AgentContext) and uses project
  MCP config when present.
- [ ] 1.3 `hyperagent0/sandbox/srt.py:_ensure_profile` reads project
  `network.allow` + global `sandbox_network_default`, writes union
  into the per-project srt profile.
- [ ] 1.4 `python/helpers/skills.py:list_skills` rewrite per D3.
- [ ] 1.5 Knowledge wiring verification (read code, add test if missing).
- [ ] 1.6 Settings field `sandbox_network_default` in `python/helpers/settings.py`.
- [ ] 1.7 Project schema migration: existing `project.json` files
  without `network` field get `{"allow": []}` on first save (no
  behavior change without explicit allowlist).

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
launch — this spec finally lands it. The skills-leak bug-fix from
the user's #2 question is a P1 task here (not a one-off fix) because
it should be done together with the per-project skills/MCP changes
that share the same project-resolution helper.
