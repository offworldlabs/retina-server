# syntax=docker/dockerfile:1
# The pragma pins the Dockerfile frontend rather than leaving it to whatever the
# building daemon happens to bundle. The uv install below is a BuildKit feature,
# and the two build paths that matter, CI and `docker compose up -d --build` on
# the droplets, are both BuildKit-backed but not both the same version. Docker
# Hub is already a build-time dependency through the base images, so this adds
# no new one.

ARG UV_VERSION=0.12.5

# ── Stage 1: Web dependencies ───────────────────────────────────────────────
# One `npm ci` for both Vite apps, from the lockfile CI installs, so the bundles
# that ship are built from the tree CI tested. `npm install` would be free to
# re-resolve and rewrite the lockfile.
FROM node:20-alpine AS web-deps
WORKDIR /app
# Manifests first, so a source-only change leaves the install layer cached.
COPY package.json package-lock.json .npmrc ./
# One line per workspace, beside the list in package.json: the lockfile names
# each one, so `npm ci` refuses a manifest that is missing here.
COPY frontend/package.json frontend/
COPY dashboard/package.json dashboard/
COPY packages/shared/package.json packages/shared/
RUN npm ci
# Vite's TypeScript transform follows each app's tsconfig `extends` chain, so
# the build needs this even though nothing here runs tsc.
COPY tsconfig.base.json ./
# Both apps import from it, so it belongs to the shared layer beneath them.
COPY packages/ packages/

# ── Stages 1a and 1b: the two builds, from one install ──────────────────────
# Separate stages so each app's build is cached and scheduled on its own: a
# source change in one app does not rebuild the other (a dependency change
# rebuilds both, through the shared install), and BuildKit runs the two in
# parallel.
FROM web-deps AS frontend-build
COPY frontend/ frontend/
# The CARTO basemap key, baked into the bundle by Vite. Declared here rather
# than in the dependency stage so that changing it re-runs this build alone.
# The empty default matters: a host that has no key (or a plain `docker build`)
# produces unkeyed URLs, and so CARTO's watermarked tiles.
ARG VITE_CARTO_API_KEY=""
ENV VITE_CARTO_API_KEY=${VITE_CARTO_API_KEY}
RUN npm run build -w frontend

FROM web-deps AS dashboard-build
COPY dashboard/ dashboard/
# Twice, because one bundle is served at two different mount points and Vite
# bakes the asset prefix in at build time. `dist` is rooted at `/` for the admin
# vhost; `dist-dash` is rooted at `/dash/` for the app vhost's mount. A single
# relative-base build would resolve its assets against the CURRENT path, which
# breaks the moment a route is more than one segment deep: `/dash/nodes/:nodeId`
# would look for its JS under `/dash/nodes/assets/`.
RUN npm run build -w dashboard && npm run build:dash -w dashboard

# ── uv, for the Python installs in the production stage ─────────────────────
# A stage of its own so the version is written once. It is only ever a mount
# source, so nothing from it reaches the shipped image. UV_VERSION is declared
# at the top of the file because an ARG is only visible to a FROM when it sits
# in the global scope, ahead of every stage.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ── Stage 2: Production image ───────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /app

# Install system deps (nginx + tini for PID 1 + libcap2-bin for setcap)
RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx tini libcap2-bin && \
    rm -rf /var/lib/apt/lists/*

# Python deps, installed with the same uv command CI uses. The version behind it
# is not the same: CI takes whatever astral-sh/setup-uv gives it, this pins. That
# is tolerable because every package in requirements.txt is pinned with ==, so
# the resolver has nothing to decide.
#
# uv is bind-mounted for the duration of the RUN rather than copied in, so its
# 54 MiB never lands in a layer of the shipped image, which has no use for uv at
# runtime.
#
# --no-cache is pip's --no-cache-dir. --compile-bytecode keeps pip's default of
# shipping .pyc alongside the sources: site-packages is root-owned and the app
# runs as appuser, so whatever is left uncompiled here can never be written at
# runtime and is recompiled on every boot.
COPY backend/requirements.txt ./
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv pip install --system --no-cache --compile-bytecode -r requirements.txt

# Submodule packages (retina_geolocator + retina_tracker)
COPY libs/retina-geolocator/ ./libs/retina-geolocator/
COPY libs/retina-tracker/ ./libs/retina-tracker/
COPY libs/retina-custody/ ./libs/retina-custody/
COPY libs/retina-simulation/ ./libs/retina-simulation/
COPY libs/retina-analytics/ ./libs/retina-analytics/
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv pip install --system --no-cache --compile-bytecode ./libs/retina-geolocator ./libs/retina-tracker ./libs/retina-custody ./libs/retina-simulation ./libs/retina-analytics

# Backend code
COPY backend/ ./backend/

# Built web apps
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist
COPY --from=dashboard-build /app/dashboard/dist /app/dashboard/dist
COPY --from=dashboard-build /app/dashboard/dist-dash /app/dashboard/dist-dash

# Rate-limit zones — http{} context, identical in every environment.
COPY deploy/nginx-security.conf /etc/nginx/conf.d/security.conf

# The vhosts themselves are NOT baked in: at boot start.sh runs
# deploy/render-nginx-config.py over deploy/nginx/nginx.conf.template, so one
# template serves staging and production and they cannot drift apart. A
# placeholder is installed here only so the file exists to be chowned below.
RUN echo "# replaced at boot by deploy/start.sh" > /etc/nginx/sites-available/default

# Deploy scripts + nginx template/snippets
COPY deploy/ /app/deploy/
RUN chmod +x /app/deploy/start.sh

# Save a pristine copy of source-controlled config files outside the
# /app/backend/config volume so they always reflect the current image.
# nodes_config.json is runtime-editable and stays in the volume; constants.py
# is source code and must follow the image.
#
# Layout: /app/deploy/config-image/config/constants.py (no __init__.py so
# Python treats 'config' as a namespace package and merges all 'config/'
# dirs on sys.path).  start.sh prepends /app/deploy/config-image to
# PYTHONPATH so this copy takes priority over the potentially-stale volume
# copy at /app/backend/config/constants.py — even when the volume is
# root-owned and the cp refresh fails.
RUN mkdir -p /app/deploy/config-image/config && \
    cp /app/backend/config/constants.py /app/deploy/config-image/config/constants.py

# ── Non-root user ────────────────────────────────────────────────────────────
RUN useradd -r -s /usr/sbin/nologin appuser && \
    # Allow nginx to bind to privileged ports as non-root
    setcap cap_net_bind_service=+ep /usr/sbin/nginx && \
    # nginx runtime dirs
    chown -R appuser:appuser /var/log/nginx /var/lib/nginx /run && \
    # allow start.sh to swap nginx config at runtime (for staging/test envs)
    chown appuser:appuser /etc/nginx/sites-available /etc/nginx/sites-available/default && \
    # app dirs that need write access
    mkdir -p /app/backend/coverage_data /app/backend/tar1090_data /app/backend/data && \
    chown -R appuser:appuser /app/backend/coverage_data /app/backend/tar1090_data /app/backend/data && \
    # /app/backend/config is mounted as a named volume; set appuser ownership
    # on the image layer so that freshly-created volumes inherit the right owner.
    chown appuser:appuser /app/backend/config

USER appuser

EXPOSE 80 443

ENTRYPOINT ["tini", "--"]
CMD ["/app/deploy/start.sh"]
