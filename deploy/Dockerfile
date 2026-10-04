# syntax=docker/dockerfile:1.7
# Heimdall images (task 063, ADR-009). One file, three targets:
#   prod - production build: no dev deps, minified UI without source maps, non-root, read-only ready.
#          Deployed to BOTH the dev VM (HEIMDALL_ENV=dev) and prod (HEIMDALL_ENV=prod): the image tested
#          on dev is promoted to prod unchanged, never rebuilt.
#   dev  - development build: dev deps, tests, UI with source maps (local work, CI test runs).
#   edge - Caddy with the built UI: static files and basemap behind forward_auth, proxy for /api.
# Base images are pinned by version; Dependabot bumps them.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG NODE_IMAGE=node:22-bookworm-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.12
ARG CADDY_IMAGE=caddy:2.10-alpine

# ---------------------------------------------------------------- web UI
FROM ${NODE_IMAGE} AS web-base
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY web/ ./

FROM web-base AS web-prod
RUN npx tsc --noEmit && npx vite build --mode production

FROM web-base AS web-dev
RUN npx vite build --mode development --sourcemap

# ---------------------------------------------------------------- python
FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS py-base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
COPY --from=uv /uv /usr/local/bin/uv
RUN groupadd --gid 10001 heimdall \
 && useradd --uid 10001 --gid heimdall --home-dir /app --shell /usr/sbin/nologin heimdall
WORKDIR /app
COPY pyproject.toml uv.lock ./

FROM py-base AS prod-build
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY heimdall ./heimdall
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# ---------------------------------------------------------------- prod (no uv, no compilers, no tests)
FROM ${PYTHON_IMAGE} AS prod
RUN groupadd --gid 10001 heimdall \
 && useradd --uid 10001 --gid heimdall --home-dir /app --shell /usr/sbin/nologin heimdall
WORKDIR /app
COPY --from=prod-build /app/.venv /app/.venv
COPY --from=prod-build /app/heimdall /app/heimdall
COPY pyproject.toml ./
COPY config ./config
COPY scripts/access_codes.py ./scripts/access_codes.py
COPY --from=web-prod /web/dist ./web/dist
RUN mkdir -p /app/data && chown heimdall:heimdall /app/data
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app HEIMDALL_ENV=prod
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/api/ready', timeout=4).status == 200 else 1)"]
CMD ["python", "-m", "heimdall.run"]

# ---------------------------------------------------------------- dev
FROM py-base AS dev
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project
COPY heimdall ./heimdall
COPY config ./config
COPY scripts ./scripts
COPY tests ./tests
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen
COPY --from=web-dev /web/dist ./web/dist
RUN mkdir -p /app/data && chown -R heimdall:heimdall /app/data
ENV PATH=/app/.venv/bin:$PATH HEIMDALL_ENV=dev
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "heimdall.run"]

# ---------------------------------------------------------------- edge
FROM ${CADDY_IMAGE} AS edge
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=web-prod /web/dist /srv/web
