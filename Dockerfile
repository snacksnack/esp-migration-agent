# syntax=docker/dockerfile:1
# Hosted review-gate demo (RC1-561), same single-image shape as school-search:
# node builds the SPA, python serves it same-origin with the FastAPI backend.
# The image bakes the fixture and the committed draft spec; the filesystem is
# ephemeral on purpose - a machine restart resets the demo to pristine.

# --- stage 1: build the web app ---
FROM node:22-slim AS web
WORKDIR /web
COPY review_ui/frontend/package.json review_ui/frontend/package-lock.json ./
RUN npm ci
COPY review_ui/frontend/ ./
RUN npm run build

# --- stage 2: python runtime ---
FROM python:3.12-slim AS runtime
RUN pip install --no-cache-dir uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
# --group ui: fastapi/uvicorn are optional for the pipeline, required here.
RUN uv sync --frozen --no-dev --group ui

COPY specs/ ./specs/
COPY ingest/ ./ingest/
COPY discovery/ ./discovery/
COPY mapping/ ./mapping/
COPY telemetry/ ./telemetry/
COPY review_ui/__init__.py review_ui/__main__.py review_ui/api.py review_ui/auth.py review_ui/config.py ./review_ui/
COPY fixtures/ ./fixtures/
COPY --from=web /web/dist ./review_ui/frontend/dist

# Git metadata for Datadog source-code integration. Declared after the
# expensive build steps so a new commit sha busts only this layer.
ARG GIT_SHA=""

ENV ESP_ENVIRONMENT=production \
    DD_GIT_COMMIT_SHA=$GIT_SHA \
    DD_GIT_REPOSITORY_URL=https://github.com/snacksnack/esp-migration-agent \
    DD_VERSION=$GIT_SHA

EXPOSE 8080
# --no-sync: `uv run` re-syncs by default, which needs groups absent from
# this image; without the flag the image builds green and fails on boot
# (the school-search lesson).
CMD ["uv", "run", "--no-sync", "python", "-m", "review_ui", "--host", "0.0.0.0", "--port", "8080"]
