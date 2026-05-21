# hyperagent0 full-stack image (spec 01 host-first, container deployment flavor).
#
# Differs from upstream's docker/run/ in two ways:
#   - Debian slim base instead of Kali (smaller, no pentest tooling baked in).
#   - Bundles only what the daemon + agent need to run; code-execution sandbox
#     is a separate image (docker/sandbox/Dockerfile, spec 05).
#
# Build from repo root:
#   docker build -f docker/hyperagent0/Dockerfile -t bayeslearner/hyperagent0:dev .
#
# Run:
#   docker run --rm -p 50080:50080 -v hyperagent0-data:/app/memory bayeslearner/hyperagent0:dev

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

# Copy dependency manifests + constraints first for layer caching.
COPY pyproject.toml README.md LICENSE ./
COPY requirements.txt requirements2.txt requirements-hyperagent0.txt ./
COPY docker/hyperagent0/constraints.txt ./docker-constraints.txt
# pyproject.toml's [tool.setuptools.packages.find] needs the packages to exist
# at install time. Copy them before pip install.
COPY hyperagent0 ./hyperagent0
COPY python ./python

# Install uv (Rust-built resolver, what upstream uses in install_A0.sh).
# pip's SAT-solver thrashes on this dep graph (scipy → backtrack → source-build
# scipy 1.11 → needs gfortran → fail). uv handles it cleanly.
RUN pip install --no-cache-dir uv

# Three-step install via uv. Each `uv pip install` freezes its results as
# constraints for the next — protects against backtracking across boundaries.
# Step 1: CPU torch stack.
# Step 2: rest of the wheels via [all] extras.
# Step 3: requirements2.txt (separate because browser-use openai conflict).
# --index-strategy unsafe-best-match: let uv check both pypi and pytorch-cpu
# index for each package (default 'first-index' is a dep-confusion safeguard
# that doesn't apply when both indexes are trusted public registries).
ENV UV_INDEX_STRATEGY=unsafe-best-match \
    UV_INDEX_URL=https://pypi.org/simple/ \
    UV_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu

RUN uv pip install --no-cache --system \
        "torch==2.7.1+cpu" "torchvision==0.22.1+cpu" \
 && uv pip install --no-cache --system \
        --constraint ./docker-constraints.txt \
        ".[all]" \
 && uv pip install --no-cache --system \
        --constraint ./docker-constraints.txt \
        -r requirements2.txt

# Now copy the rest of the runtime tree. Source files agent.py / models.py /
# run_ui.py / initialize.py are imported at startup by hyperagent0.cli_commands.start,
# and runtime asset dirs (prompts/, knowledge/, agents/, webui/, skills/) are
# looked up by path. .dockerignore strips logs/tmp/usr/.git/.venv.
COPY agent.py initialize.py models.py preload.py prepare.py run_ui.py run_tunnel.py ./
COPY agents ./agents
COPY prompts ./prompts
COPY knowledge ./knowledge
COPY webui ./webui
COPY skills ./skills
COPY conf ./conf
COPY lib ./lib

# Trim build-only apt packages and pip cache. Saves ~250MB.
RUN apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

# Mount points for persistent state. memory/ holds vector DBs, usr/ holds
# projects + secrets, logs/ holds daemon logs.
RUN mkdir -p /app/memory /app/usr /app/logs /app/tmp
VOLUME ["/app/memory", "/app/usr", "/app/logs"]

EXPOSE 50080

# --systemd: foreground, no PID file (correct for PID 1 in container).
CMD ["haz", "start", "--systemd", "--host", "0.0.0.0", "--port", "50080"]
