# syntax=docker/dockerfile:1
# 𝚅𝚒𝚋𝚎𝚌𝚛𝚊𝚏𝚝𝚎𝚍. with AI Agents by Vetcoders (c)2024-2026 LibraxisAI
#
# Screenscribe Cloud image. One image, three roles selected by the command:
#   serve    HTTP API, portal and hosted reviews (uvicorn, proxy headers on)
#   worker   durable pipeline worker (polls SQLite, runs ffmpeg/ffprobe)
#   init-db  idempotent schema create/migrate, then exit
#
# Build context is the repository root:
#   docker build -f deploy/Dockerfile -t screenscribe-cloud:local .
# Only the explicit paths below are copied, so local secrets such as
# .env.cloud never enter an image layer.
#
# Bind address: the application defaults to loopback (127.0.0.1) and this
# image deliberately does NOT set SCREENSCRIBE_CLOUD_HOST. The compose file
# (deploy/docker-compose.prod.yml) widens it to all interfaces on the `web`
# service only, where the port is reachable solely from Caddy on the private
# compose network. A bare `docker run` of this image stays loopback-only.

ARG PYTHON_IMAGE=python:3.11-slim-bookworm

# ── Builder: resolve the locked dependency set into /opt/venv ────────────────
FROM ${PYTHON_IMAGE} AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /src

# Project metadata plus everything the hatchling backend reads at build time:
# README.md (readme), LICENSE (license-files) and hatch_build.py (custom build
# hook; it skips commit provenance cleanly when git is absent, as it is here).
COPY pyproject.toml uv.lock README.md LICENSE hatch_build.py ./

# Dependencies first, so source edits do not invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --no-install-project

COPY screenscribe ./screenscribe

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ── Runtime: slim base + ffmpeg, non-root, venv only ────────────────────────
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="screenscribe-cloud" \
      org.opencontainers.image.description="Screenscribe Cloud: hosted screencast review backend and worker" \
      org.opencontainers.image.source="https://github.com/vetcoders/screenscribe" \
      org.opencontainers.image.vendor="Vetcoders" \
      org.opencontainers.image.licenses="BUSL-1.1"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

# System user 10001 owns the data directory. A fresh named volume mounted at
# /var/lib/screenscribe/data inherits this ownership from the image.
RUN groupadd --system --gid 10001 screenscribe \
    && useradd --system --uid 10001 --gid 10001 \
        --home-dir /var/lib/screenscribe --create-home \
        --shell /usr/sbin/nologin screenscribe \
    && install -d -o 10001 -g 10001 -m 0750 /var/lib/screenscribe/data

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/var/lib/screenscribe \
    SCREENSCRIBE_CLOUD_STORAGE_DIR=/var/lib/screenscribe/data \
    SCREENSCRIBE_CLOUD_PORT=8080

WORKDIR /var/lib/screenscribe
USER 10001:10001
EXPOSE 8080

# Liveness of the `serve` role. The probe hits loopback inside the container,
# which works for both the loopback default and the compose-widened bind.
# The worker and init-db services disable this check in compose.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["tini", "--", "python", "-m", "screenscribe.cloud"]
CMD ["serve"]
