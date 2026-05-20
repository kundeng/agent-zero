# hyperagent0 sandbox image — used by DockerBackend / PodmanBackend.
# Stays tiny: python3 + minimal toolchain for LLM-generated code to run in.
FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl jq build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir ipython
WORKDIR /workspace
CMD ["sleep", "infinity"]
