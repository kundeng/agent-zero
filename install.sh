#!/usr/bin/env bash
# HyperAgent Zero — one-command host installer.
#
# Run remotely (no repo clone needed):
#
#   curl -fsSL https://raw.githubusercontent.com/kundeng/hyperagent-zero/v2-hyperagent/install.sh | bash
#
# Or, if you already have the repo (developer path):
#
#   git clone https://github.com/kundeng/hyperagent-zero
#   cd hyperagent-zero
#   ./install.sh --dev          # editable install pointing at this checkout
#
# Flags:
#   --dev                 Use the current directory as the source (developer mode).
#                         Same effect as the default flow but skips the git clone
#                         and uses the cwd as REPO_DIR.
#   --prefix DIR          Install under DIR/ (default: ~/.hyperagent0).
#   --branch NAME         Clone this branch (default: v2-hyperagent).
#   --no-link             Skip the ~/.local/bin/haz symlink.
#   --extras LIST         Comma list of pip extras (default: all).
#
# hyperagent0 is always installed editable (``pip install -e``) against
# REPO_DIR. That means a future ``git pull`` of REPO_DIR upgrades the
# installed package without a reinstall step — re-running the curl|bash
# one-liner just fast-forwards the repo and exits.
#
# What gets installed:
#
#   ~/.hyperagent0/repo/     a git checkout (so the agent's runtime assets
#                            — prompts/, agents/, webui/ — have a stable home)
#   ~/.hyperagent0/venv/     the python venv with the agent installed
#   ~/.local/bin/haz         symlink to the venv's haz entry point
#   ~/.local/bin/hyperagent0 symlink (long name, same target)
#
# Install pattern mirrors upstream agent-zero's install_python.sh +
# install_A0.sh, with requirements2.txt applied LAST so its pins win.
#
# NOTHING about the LLM provider is configured here. Run `haz start`,
# open http://localhost:50080, and pick a provider in the UI.

set -euo pipefail

PREFIX="${HYPERAGENT0_PREFIX:-$HOME/.hyperagent0}"
BRANCH="v2-hyperagent"
REPO_URL="https://github.com/kundeng/hyperagent-zero"
EXTRAS="all"
DEV_MODE=0
DO_LINK=1
DEV_SRC=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dev)
            DEV_MODE=1
            DEV_SRC="$(pwd)"
            shift ;;
        --prefix)
            PREFIX="$2"; shift 2 ;;
        --prefix=*)
            PREFIX="${1#*=}"; shift ;;
        --branch)
            BRANCH="$2"; shift 2 ;;
        --branch=*)
            BRANCH="${1#*=}"; shift ;;
        --extras)
            EXTRAS="$2"; shift 2 ;;
        --extras=*)
            EXTRAS="${1#*=}"; shift ;;
        --no-link)
            DO_LINK=0; shift ;;
        -h|--help)
            sed -n '2,32p' "$0"; exit 0 ;;
        *)
            echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

REPO_DIR="${PREFIX}/repo"
VENV_DIR="${PREFIX}/venv"
BIN_DIR="${HOME}/.local/bin"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

err() { echo "error: $*" >&2; exit 1; }

command -v git >/dev/null 2>&1 \
    || err "git is required. Install it first (apt install git / brew install git)."

# Find a Python >= 3.12. Upstream uses PEP 695 syntax in agent.py so 3.11
# will not parse it.
PY=""
for candidate in python3.12 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,12) else 1)' 2>/dev/null; then
            PY="$candidate"; break
        fi
    fi
done

if [ -z "$PY" ]; then
    cat <<EOF >&2
error: python 3.12+ is required (upstream uses PEP 695 syntax).
Install it then re-run this script:

  Ubuntu 22.04 / 24.04:
      sudo add-apt-repository ppa:deadsnakes/ppa
      sudo apt update
      sudo apt install -y python3.12 python3.12-venv

  Debian 13+:
      sudo apt install -y python3.12 python3.12-venv

  macOS:
      brew install python@3.12

  Other:
      install uv (https://docs.astral.sh/uv/) then:
      uv python install 3.12

EOF
    exit 1
fi

echo "==> using $PY ($("$PY" --version))"

# ---------------------------------------------------------------------------
# Repo: clone (or update) into PREFIX/repo. Skipped in --dev mode.
# ---------------------------------------------------------------------------

mkdir -p "${PREFIX}"

if [ "${DEV_MODE}" -eq 1 ]; then
    REPO_DIR="${DEV_SRC}"
    echo "==> [1/5] using local checkout at ${REPO_DIR} (--dev)"
    if [ ! -f "${REPO_DIR}/pyproject.toml" ]; then
        err "no pyproject.toml at ${REPO_DIR} — run --dev from inside a hyperagent-zero checkout."
    fi
elif [ -d "${REPO_DIR}/.git" ]; then
    echo "==> [1/5] updating existing checkout at ${REPO_DIR}"
    git -C "${REPO_DIR}" fetch --quiet origin "${BRANCH}"
    git -C "${REPO_DIR}" checkout --quiet "${BRANCH}"
    git -C "${REPO_DIR}" reset --hard --quiet "origin/${BRANCH}"
else
    echo "==> [1/5] cloning ${REPO_URL} (branch ${BRANCH}) to ${REPO_DIR}"
    git clone --quiet --branch "${BRANCH}" --depth 1 "${REPO_URL}" "${REPO_DIR}"
fi

# ---------------------------------------------------------------------------
# Venv
# ---------------------------------------------------------------------------

if [ ! -d "${VENV_DIR}" ]; then
    echo "==> [2/5] creating venv at ${VENV_DIR}"
    "${PY}" -m venv "${VENV_DIR}"
else
    echo "==> [2/5] reusing venv at ${VENV_DIR}"
fi

PIP="${VENV_DIR}/bin/pip"
UV="${VENV_DIR}/bin/uv"

"${PIP}" install --quiet --upgrade pip

# ---------------------------------------------------------------------------
# Four-step install (mirrors docker/hyperagent0/Dockerfile)
# ---------------------------------------------------------------------------

cd "${REPO_DIR}"

echo "==> [3/5] CPU torch (this is the big download, ~200MB)"
"${PIP}" install --quiet --disable-pip-version-check \
    torch==2.4.0 torchvision==0.19.0 \
    --index-url https://download.pytorch.org/whl/cpu

echo "==> [4/5] requirements.txt + hyperagent0 + extras [${EXTRAS}]"
"${PIP}" install --quiet uv
"${UV}" pip install --quiet --python "${VENV_DIR}/bin/python" -r requirements.txt
# Editable install in BOTH dev and curl-bash flows. REPO_DIR is at a
# stable path (~/.hyperagent0/repo by default), so editable is safe —
# and it makes ``git pull`` upgrades work without a re-install step.
"${UV}" pip install --quiet --python "${VENV_DIR}/bin/python" -e ".[${EXTRAS}]"

echo "==> [5/5] applying pin overrides (requirements2.txt, runs last)"
"${UV}" pip install --quiet --python "${VENV_DIR}/bin/python" -r requirements2.txt

# Editable install means hyperagent0/__init__.py lives in the cloned
# repo on disk, not in site-packages — so a future ``git pull`` of the
# repo automatically updates the installed package. No reinstall step
# needed during routine upgrades.

# ---------------------------------------------------------------------------
# Site bootstrap: drop a .pth file so every Python invocation in this venv
# can ``import agent`` and ``hyperagent0.paths.repo_root()`` works without
# needing HYPERAGENT0_REPO in the user's shell rc.
#
# .pth file format: lines starting with ``import`` are exec'd, other lines
# are added to sys.path. Two lines, both pointing at the same checkout.
# ---------------------------------------------------------------------------

# Resolve the venv's site-packages dir without hardcoding the python minor.
SITE_PACKAGES="$("${VENV_DIR}/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
PTH_FILE="${SITE_PACKAGES}/hyperagent0_repo.pth"
ABS_REPO="$(cd "${REPO_DIR}" && pwd)"
{
    echo "import os; os.environ.setdefault('HYPERAGENT0_REPO', '${ABS_REPO}')"
    echo "${ABS_REPO}"
} > "${PTH_FILE}"

# ---------------------------------------------------------------------------
# Bin symlinks
# ---------------------------------------------------------------------------

if [ "${DO_LINK}" -eq 1 ]; then
    mkdir -p "${BIN_DIR}"
    ln -sf "${VENV_DIR}/bin/haz" "${BIN_DIR}/haz"
    ln -sf "${VENV_DIR}/bin/hyperagent0" "${BIN_DIR}/hyperagent0"
    echo "==> linked haz + hyperagent0 into ${BIN_DIR}"
fi

# Check PATH and warn if ~/.local/bin isn't on it.
PATH_WARN=""
case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) PATH_WARN="yes" ;;
esac

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

cat <<EOF

============================================================================
  HyperAgent Zero installed.
============================================================================

  Start the daemon:
      haz start                  # foreground; Ctrl-C to stop
      haz start -d               # background; haz status / haz logs / haz stop

  Then open:
      http://localhost:50080

  Configure your LLM provider in the web UI's Settings panel — same flow
  as upstream agent-zero. No CLI setup required.

EOF

if [ -n "${PATH_WARN}" ]; then
    cat <<EOF
  WARNING: ${BIN_DIR} is not on your PATH.
  Add it to your shell rc to use \`haz\` directly:

      echo 'export PATH="\$HOME/.local/bin:\$PATH"' >> ~/.bashrc   # or ~/.zshrc
      source ~/.bashrc

  Or invoke it directly: ${VENV_DIR}/bin/haz start

EOF
fi
