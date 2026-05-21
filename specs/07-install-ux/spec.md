---
spec_id: 07-install-ux
status: DRAFT
since: 2026-05-21
until: null
epic: devops
features: [curl-bash-installer, repo-clone-mechanism, journey-matrix, no-llm-during-install]
supersedes: []
superseded_by: null
depends_on: [01-host-first, 03-daemon-cli]
---

# Install UX — User Journeys

## Context

Specs 01 and 03 established that the agent runs natively and exposes a
`haz` CLI. They left open the question of *how* the bits actually land
on the user's machine. Early drafts assumed the user clones the
repository and runs an install script — which is a *developer*
workflow, not a user one. Real users don't `git clone` software they
want to use; they paste one command from a README.

This spec consolidates the four supported install paths into a
deliberately narrow matrix, defines what each one looks like from the
user's POV, and locks in the technical decisions needed to make each
one a single copy-paste command.

### The four journeys

```
                    ┌────────────────────────────────────────────────┐
                    │            What does the user want?              │
                    └─────────────────────┬─────────────────────────────┘
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              │                           │                           │
   "try it for 30s"          "use it daily on my            "stick it on a server"
              │             host or VM, agent native"                  │
              │                           │                           │
              ▼                           ▼                           ▼
       [Journey A]                 [Journey B]                  [Journey D]
       docker run                  curl | bash                  compose +
       Image only                  ~/.hyperagent0/              two-file
                                                                download
                                          │
                                          ├─→ [Journey C]
                                          │   same as B, then flip
                                          │   sandbox_mode=docker
                                          │   for per-project
                                          │   container isolation
```

Each journey ships in `README.md` as a copy-paste block. None of them
require the user to clone the repository or configure an LLM provider
at the CLI — the web UI handles LLM configuration once the daemon is
up, mirroring upstream agent-zero.

## Decisions

### D1 — No `git clone` in any user-facing flow.

End users are not developers of the project. The README's three user
journeys (A, B, D) and the fourth optional (C) MUST work without the
user typing `git clone`. The install scripts and Docker images own
whatever cloning is needed internally. Only the developer setup
section at the bottom of `README.md` mentions `git clone`.

**Why**: A clone step asks the user to make a decision about file
layout they shouldn't care about (where does the repo go? what about
permissions? etc.), and signals "this is a project to hack on" rather
than "this is a tool to install."

### D2 — No LLM configuration during install.

Mirroring upstream behavior, the install path stops at "daemon is
running, UI is reachable." The user configures their LLM provider in
the web UI's Settings panel — same flow as `agent0ai/agent-zero`.

This rules out:
- Interactive prompts during `install.sh` ("which provider? what
  API base?")
- `haz setup --quick` writing opinionated defaults
- The Docker entrypoint demanding an API key

`haz setup` still exists as an optional CLI helper, but it is NOT on
the install path and writes ONLY the fields the user passes as flags
— no opinionated defaults sprayed across the file.

**Why**: Choosing a provider is a per-user decision tied to their
existing accounts and keys. Forcing it during install creates a path
where new users either fabricate fake answers (then debug the broken
config later) or quit before getting to the UI. The UI is where
configuration belongs because it can validate, persist, and provide
context for each field.

### D3 — Install layout: `~/.hyperagent0/{repo,venv,logs,state.sock}`.

The curl|bash installer writes everything under a single top-level
directory under the user's home:

| Path | Contents |
|------|----------|
| `~/.hyperagent0/repo/` | Git checkout of the source tree (provides `agent.py`, `prompts/`, runtime assets) |
| `~/.hyperagent0/venv/` | Python virtual environment with `pip install`'d hyperagent0 + extras |
| `~/.hyperagent0/logs/` | `daemon.log`, channel logs, scheduler logs |
| `~/.hyperagent0/state.sock` | Optional Unix socket for `haz status` introspection (spec 03 P3) |
| `~/.local/bin/haz` | Symlink to `~/.hyperagent0/venv/bin/haz` |
| `~/.local/bin/hyperagent0` | Symlink (long-form name) |

No `sudo` required. No files outside `$HOME`. `~/.local/bin` is the
XDG-recommended per-user binary directory and is on PATH for most
modern shells (or one rc line away).

### D4 — The repo MUST be cloned; the wheel MUST NOT bundle `python/`.

Upstream `agent-zero` is not a proper Python package. Its code at
multiple sites does:

```python
from pathlib import Path
base = Path(__file__).parent.parent  # → repo root
config = base / "conf" / "workdir.gitignore"
```

If the wheel bundles a copy of `python/` into `site-packages/python/`,
that copy's `base` resolves to `site-packages/` — where there is no
`conf/`, no `prompts/`, no `agents/`. The daemon raises
`FileNotFoundError` on the very first request.

Resolution:
1. Wheel ships ONLY the `hyperagent0/` package. `pyproject.toml`'s
   `[tool.setuptools.packages.find].include = ["hyperagent0*"]`,
   `exclude = [..., "python*"]`.
2. install.sh clones the full repo to `~/.hyperagent0/repo/`.
3. install.sh writes a two-line `.pth` file into the venv's
   site-packages:

   ```
   import os; os.environ.setdefault('HYPERAGENT0_REPO', '<absolute repo path>')
   <absolute repo path>
   ```

   Line 1 (executable, starts with `import`) sets the env var.
   Line 2 (path-only) adds the repo to `sys.path`.

4. The daemon imports `python.helpers.files` etc. from
   `~/.hyperagent0/repo/python/` via sys.path. `files.get_base_dir()`
   resolves to the repo root, so `conf/workdir.gitignore` and
   friends are reachable.

The Docker image escapes this entirely by `COPY`-ing the whole tree
into `/app/` and setting `PYTHONPATH=/app`. Same outcome, different
mechanism.

**Why .pth and not env-var-in-shell-rc**: A `.pth` runs at every
Python invocation in that venv, with no user action required after
install. Editing the user's `.bashrc` would be invasive and only work
for that one shell.

### D5 — Standardize on port 50080 across all journeys.

Upstream defaults to port 5000. Our four journeys all advertise port
50080 because:
- Docker images already use 50080 (set via `WEB_UI_PORT=50080`)
- 5000 collides with macOS AirPlay Receiver
- A single port number in the README is one less thing to get wrong

`haz start` defaults to 50080 when `WEB_UI_PORT` is unset and `--port`
isn't passed. Users who explicitly want 5000 can pass `--port 5000`
or set `WEB_UI_PORT=5000`.

### D6 — Path resolver lives in `hyperagent0/paths.py`.

Subcommands that need the repo path (`haz config`, `haz setup`, `haz
start`'s sys.path injection) call a single `repo_root()` that resolves
in this order:

1. `$HYPERAGENT0_REPO` env var if set.
2. Walk up from `paths.py`'s own `__file__` for an `agent.py` +
   `pyproject.toml` pair. Handles editable installs and developer
   checkouts naturally.
3. `~/.hyperagent0/repo` if it contains `agent.py`. The default
   curl|bash install location.
4. Raise `RepoNotFound` with a fix hint.

`hyperagent0/cli.py` calls `paths.ensure_on_syspath()` at module load
so subcommands that `import agent` don't need to think about it.
Stdlib-only (no `litellm` etc. on the path) so the cold-start budget
from spec 03 D5 holds.

### D7 — `install.sh` IS the API.

The script at `<repo>/install.sh` is the entry point for Journey B
(and C, with one extra command). It is fetched verbatim by the
README's curl one-liner and must therefore be:

- Idempotent — re-running upgrades in place
- Self-bootstrapping — accepts being run with no working directory
  context (piped from curl)
- Diagnose-not-guess — fails fast with clear messages when Python
  3.12 / git / network / disk space are missing
- `--dev` aware — running it from inside a developer checkout
  produces an editable install pointing at that checkout
- Free of LLM/secret prompts (per D2)

The script's flags (`--prefix`, `--branch`, `--extras`, `--no-link`,
`--dev`) are part of the public surface and only change in
spec-tracked ways.

## Tasks

### P0 — Done (this spec's foundation)
- [x] 0.1 `install.sh` rewritten as a curl|bash bootstrap
- [x] 0.2 `hyperagent0/paths.py` resolves repo root from env/walk-up/default
- [x] 0.3 `pyproject.toml` excludes `python*` from the wheel
- [x] 0.4 install.sh writes `.pth` file injecting `HYPERAGENT0_REPO` + sys.path
- [x] 0.5 `haz config set KEY VALUE` added (was: get + path only)
- [x] 0.6 `haz setup --quick` removed (replaced by flag-only semantics)
- [x] 0.7 `haz start` default port = 50080
- [x] 0.8 README rewritten around the four journeys + a Developer section
- [x] 0.9 Verified end-to-end in a clean multipass Ubuntu 22.04 VM via
       the real curl|bash URL

### P1 — Must Do
- [ ] 1.1 Docker image SHOULD honor `WEB_UI_HOST=0.0.0.0` so the
       compose deployment reaches the LAN. (`haz start --systemd
       --host 0.0.0.0` already does this; verify env var path.)
- [x] 1.2 `haz start --lan` — shortcut for `--host 0.0.0.0`. Same
       effect, less typing, discoverable from `haz start --help`.
       Documented in README's "LAN access" subsection.
- [x] 1.3 `haz check` subcommand: reads settings.json, makes a
       minimal LiteLLM call, prints OK + latency or a one-line
       diagnosis. Exit codes 0/1/2/3/4 cover reachable / no config
       / missing field / network-or-auth fail / unsupported
       provider. claude-sdk path stubbed (exit 4) until spec 02
       adds a real probe.
- [ ] 1.4 Test: end-to-end curl|bash install in CI (GitHub Actions
       Ubuntu runner). Replays journey B without LLM keys, asserts
       UI returns 200 on /. Catches regressions of D4.
- [ ] 1.5 Test: wheel build excludes `python/`. Build the wheel,
       open it as a zip, assert there is no `python/` directory.
- [ ] 1.6 Document the upgrade path inside `README.md` — `curl ... |
       bash` on an existing install does `git pull` in
       `~/.hyperagent0/repo` and reinstalls into the existing venv.
       Already supported by install.sh's "reuse existing venv"
       branch; just needs the README hint.

### P2 — Should Do
- [ ] 2.1 Compose `.env.example` and `docker-compose.yml` accept
       `SANDBOX_MODE=docker` so journey D can opt into per-project
       container isolation without editing settings.json inside the
       container.
- [ ] 2.2 Optional: PyPI publish. Once stable enough for that, the
       curl|bash install can pivot to `pipx install hyperagent0`
       (still with a post-install hook that clones the repo for
       runtime assets). Lower priority — the current flow works and
       PyPI's name-squatting risk for `hyperagent0` is low.
- [ ] 2.3 `haz uninstall` subcommand: remove `~/.hyperagent0`, the
       symlinks in `~/.local/bin/`, and emit a one-line summary of
       what was removed. Currently users have to know which dirs to
       `rm -rf`.

### P3 — Nice to Have
- [ ] 3.1 macOS-specific install path: the `add-apt-repository`
       instruction in install.sh's preflight is Linux-only. Surface
       `brew install python@3.12` more prominently.
- [ ] 3.2 Air-gapped install: provide a tarball release containing
       the cloned repo + a pre-built venv (or a uv-managed lock
       file). Out of scope for normal users but a real ask for some
       corporate deployments.

## Open Questions

- [ ] **The repo MUST be a clone.** Are we sure? The alternative
       (`pipx install` with all assets bundled in the wheel) would
       require turning `prompts/`, `agents/`, `webui/` etc. into
       `package_data` and reshaping upstream's `get_base_dir()`
       logic — a far larger upstream-divergence than D4 incurs.
       Leaning toward keeping the clone permanently. Revisit only
       if upstream itself moves to a package layout.

- [ ] **Should install.sh handle Python 3.12 installation?** Today
       it errors with a clear OS-specific hint. We could shell out
       to `uv python install 3.12` if `uv` is on PATH, or even
       download `uv` if not. Adds a dep on uv as a system tool but
       removes the "deadsnakes" friction on Ubuntu 22.04. Worth
       prototyping behind a `--auto-python` flag.

- [ ] **What's the upgrade story when ``python/`` exclude is itself
       backwards-incompatible?** D4 means existing
       wheel-from-PyPI installs are broken even if such installs
       existed (they don't — we never published). Once on PyPI, a
       version bump that changes wheel contents is fine; the issue
       is only that older curl|bash installs may have site-packages
       residue. install.sh's `--force-reinstall` flag (TBD) would
       handle this; we could also just instruct users to delete
       `~/.hyperagent0` and re-curl.

## Log

**2026-05-21** — Spec created after end-to-end install testing
surfaced two design assumptions that didn't hold. (1) Original install
flow required `git clone` for the user; rewritten as curl|bash with
internal clone. (2) Original `haz setup --quick` wrote proxy LLM
defaults — the user's local config — into every fresh install;
removed in favor of flag-only writes. Also caught the wheel-bundling
`python/` bug (D4) during the VM test: the curl|bash install installed
cleanly but `haz start` died on `FileNotFoundError:
site-packages/conf/workdir.gitignore`. Fixed by excluding `python*`
from the wheel and clarifying that the repo clone is load-bearing for
runtime asset resolution. Verified the full flow against the public
GitHub URL in a multipass Ubuntu 22.04 VM: curl → install → `haz start
-d` → UI returns 200 on `http://localhost:50080/`.

**2026-05-21** — Landed P1.2 (`haz start --lan` flag) and P1.3
(`haz check` subcommand). `--lan` is a one-line shortcut for
`--host 0.0.0.0`; mutually exclusive with explicit `--host` to
avoid silently overriding a user's intent. `haz check` reads
`usr/settings.json` and makes a minimal LiteLLM round-trip with
`max_tokens=8`, exiting 0 with a one-line "OK (Xs)" on success or
a nonzero code + diagnosis on failure (1 = no config, 2 = missing
field, 3 = LLM error, 4 = unsupported provider). 8 unit tests
cover the no-network code paths (settings missing, fields missing,
flag overrides, provider name prefix logic). Heavy LiteLLM imports
are deferred into the command body so `haz check --help` still
satisfies the cold-start budget (only Click is imported).
