# The Docker route (UNTESTED: written without a Docker engine available; see README).
# One container runs the relay, the MiroFish backend and frontend, and the socket sampler, exactly as
# `python -m ops start` does on a laptop. bootstrap runs at build time: it clones upstream at the pinned
# commit, applies ops/patches, installs locked dependencies and caches the one-time downloads, so the
# running container only talks to Gemini and Zep. No key or private config is copied into the image
# (see .dockerignore); keys come from the host environment at run time, config is mounted read-only.
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /usr/local/bin/

RUN useradd --create-home --uid 10001 mirofish
WORKDIR /app
COPY --chown=mirofish:mirofish ops/ ops/
COPY --chown=mirofish:mirofish .env.example ./
USER mirofish

ENV MIROFISH_OPS_CONTAINER=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN python -m ops bootstrap --skip-tests

EXPOSE 3000 5001
CMD [".venv/bin/python", "-m", "ops", "start", "--foreground"]
