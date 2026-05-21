# hyperagent0 sandbox image — execution target for spec 05's DockerBackend
# and PodmanBackend. Stays tiny: the agent shells in via `docker exec` (or
# paramiko/SSH) and runs arbitrary code, so all this image needs is a clean
# runtime with the language tools agents commonly ask for.
#
# Notably absent: agent.py, hyperagent0/, requirements.txt. The agent never
# runs inside this image; only its generated code does.
#
# Build:  docker build -f docker/sandbox/Dockerfile -t bayeslearner/hyperagent0-sandbox:latest docker/sandbox
# Run:    spec 05 backends spawn this with `docker run -d --rm` and exec into it.

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Common toolchain for LLM-generated code:
#   git, curl, jq, ca-certificates  -> fetch + inspect remote resources
#   build-essential                 -> compile native pip wheels on demand
#   nodejs, npm                     -> JS skills + npx
#   bash                            -> default shell for `docker exec`
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        bash \
        git \
        curl \
        jq \
        ca-certificates \
        build-essential \
        nodejs \
        npm \
 && rm -rf /var/lib/apt/lists/*

# ipython makes interactive shells inside the sandbox much nicer; everything
# else agents need they install themselves via `pip install <x>` at run time.
RUN pip install --no-cache-dir ipython

WORKDIR /workspace

# Keep the container alive so the agent can `docker exec` into it on demand.
CMD ["sleep", "infinity"]
