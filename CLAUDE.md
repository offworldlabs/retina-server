# retina-server

FastAPI backend and React front-ends for the RETINA passive-radar network.

- [`README.md`](README.md): what this is, quick start, and where the tower-search
  API went (tower-finder-service — not this backend).
- [`ONBOARDING.md`](ONBOARDING.md): clone to running stack, tests, how code ships,
  and the things that will bite you. Read it before changing anything here.
- [`docs/`](docs/): architecture, pipeline, runbook, alerting, simulation, arc display.
- [claude-shared](https://github.com/offworldlabs/claude-shared/tree/main/docs):
  org-wide architecture, contracts, decisions and runbooks.

## Working in this repo

- **Verify with the gate, not by hand.** `backend/.venv/bin/pre-commit run --all-files`
  before pushing, and `git add` new files first so the hooks can see them. See
  ONBOARDING, "Before you push", for what it runs and where it lies to you.
- **Every PR runs the full matrix**, whatever it is based on. Branches opened
  before #187 predate that and ran nothing unless they targeted `main`.
- **The node API contract is generated.** Change a route under `/v1/nodes`, one of
  its models, or a configuration bound in `backend/services/node_config.py`, and
  `contracts/nodes-v1.openapi.yaml` moves with it; regenerate it in the same commit
  or CI fails. See ONBOARDING, "Before you push".
- **`backend/uv.lock` moves with its inputs.** Change a dependency in
  `backend/pyproject.toml`, or bump a submodule whose lib changed its own
  dependencies, and relock in the same commit, or CI fails at
  `uv sync --locked`. Relock with the uv the `Dockerfile` pins, which CI runs:
  `uv tool run uv@<UV_VERSION> lock` from `backend/`. See ONBOARDING, "Backend".
- **This repo is public.** Refer to hosts by SSH alias, never by address, as
  `justfile` already does. No credentials, no droplet addresses, no personal
  accounts in anything committed here.
- **Configuration lives in `backend/.env`**, which is gitignored. Add new keys to
  `backend/.env.example` so the list stays current.
- **Verify a deploy from the environment's API.** Green tests do not cover the
  compose/env/frontend seams, so confirm the change against the environment
  itself before calling a deploy done: `aircraft_on_map` in
  `/api/test/dashboard`, the node set in `/api/radar/analytics`, and
  `/api/radar/data/aircraft.json` for the data path. Scripted requests need a
  browser User-Agent or Cloudflare answers `403 1010`.
