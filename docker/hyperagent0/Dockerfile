# Full HyperAgent Zero image. Bundles the agent daemon + all extras.
# Code-execution sandboxing uses a separate slim image (../sandbox/Dockerfile),
# spawned per-project by spec 05's docker/podman backends.
#
# Build from repo root:
#   docker build -f docker/hyperagent0/Dockerfile -t bayeslearner/hyperagent0:latest .
# Run:
#   docker run --rm -p 50080:50080 -v hyperagent0-data:/app/memory bayeslearner/hyperagent0:latest
#
# Install pattern mirrors upstream's docker/base/fs/ins/install_python.sh
# and docker/run/fs/ins/install_A0.sh — sequential pip resolutions with the
# override file applied LAST so its pins win:
#
#   1. CPU torch from the pytorch index (isolated --index-url, not --extra)
#   2. uv pip install -r requirements.txt          (gets browser-use → openai 1.99.2)
#   3. uv pip install ".[all]"                     (our extras + entry points)
#   4. uv pip install -r requirements2.txt         (forces openai 1.99.5)
#
# No constraints file. No --index-strategy magic. Order matters: step 4 runs
# AFTER ".[all]" because the [all] resolution will otherwise downgrade openai
# back to browser-use's transitive pin. requirements2.txt is the final word.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    WEB_UI_HOST=0.0.0.0 \
    WEB_UI_PORT=50080 \
    PYTHONPATH=/app

# System packages:
#   build-essential, git, curl  -> wheel builds + git-based pip installs (removed post-install)
#   ffmpeg                      -> openai-whisper audio I/O
#   espeak-ng, libsndfile1      -> kokoro / phonemizer / soundfile
#   tesseract-ocr               -> pytesseract OCR
#   poppler-utils               -> pdf2image PDF rasterization
#   libmagic1                   -> python-magic file type detection
#   ca-certificates             -> TLS
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        curl \
        ca-certificates \
        ffmpeg \
        espeak-ng \
        libsndfile1 \
        tesseract-ocr \
        poppler-utils \
        libmagic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Manifests + python source first for layer caching. pyproject.toml's
# [tool.setuptools.packages.find] resolves the package list at install time,
# so the hyperagent0/ and python/ trees must exist before `pip install .`.
COPY pyproject.toml README.md LICENSE ./
COPY requirements.txt requirements2.txt requirements-hyperagent0.txt ./
COPY hyperagent0 ./hyperagent0
COPY python ./python

# Step 1: CPU torch wheels from the dedicated pytorch index. Versions match
# upstream install_python.sh for consistency with their tested set. Using
# --index-url (not --extra-index-url) keeps this install fully isolated to
# the pytorch index, which is what produces +cpu local-version wheels.
RUN pip install --no-cache-dir \
        torch==2.4.0 torchvision==0.19.0 \
        --index-url https://download.pytorch.org/whl/cpu

# Step 2: uv (Rust-built resolver) + the main requirements file. This
# installs browser-use, litellm, and the rest. openai will be at
# browser-use's transitive pin (1.99.2) after this step — fixed below.
RUN pip install --no-cache-dir uv \
 && uv pip install --no-cache --system -r requirements.txt

# Step 3: our wrapper package + opt-in extras. Registers the haz /
# hyperagent0 entry points and adds anthropic, claude-agent-sdk, docker,
# podman, and channel SDKs.
RUN uv pip install --no-cache --system ".[all]"

# Step 4: override pins. requirements2.txt contains explicit upgrades
# (openai==1.99.5) that win against the transitive pins set above. Runs
# last so subsequent resolutions cannot undo it.
RUN uv pip install --no-cache --system -r requirements2.txt

# Runtime source tree. Copied after deps so dep layers cache when only
# source changes. .dockerignore strips logs/, tmp/, usr/, .git/, .venv/.
COPY agent.py initialize.py models.py preload.py prepare.py run_ui.py run_tunnel.py ./
COPY agents ./agents
COPY prompts ./prompts
COPY knowledge ./knowledge
COPY webui ./webui
COPY skills ./skills
COPY conf ./conf
COPY lib ./lib

# Trim build-only apt packages and resolver caches. Saves ~300MB.
RUN apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip /root/.cache/uv

# Mount points for persistent state. memory/ holds vector DBs, usr/ holds
# projects + secrets, logs/ holds daemon logs.
RUN mkdir -p /app/memory /app/usr /app/logs /app/tmp
VOLUME ["/app/memory", "/app/usr", "/app/logs"]

EXPOSE 50080

# --systemd: foreground, no PID file (correct for PID 1 in container).
CMD ["haz", "start", "--systemd", "--host", "0.0.0.0", "--port", "50080"]
