# retina-server

This repo powers RETINA, a passive-radar system: receiver nodes detect aircraft
from reflections of broadcast transmitters, and the backend turns those
detections into live tracks shown on a web map. "Tower Finder" — the original
illuminator-search feature — is one of several surfaces (along with the live map
and the admin dashboard), and is the one whose API now lives in its own service.

> **New here?** Start with [`ONBOARDING.md`](ONBOARDING.md) for the full picture
> and local setup, and [`docs/architecture.md`](docs/architecture.md) for how the
> pieces fit together.

## Tower Finder feature

Web application that helps passive radar operators find suitable broadcast tower
illuminators near their location: given coordinates, it returns nearby FM/VHF/UHF
transmitters ranked by suitability for passive radar use.

This repo owns the SPA (`frontend/`) and the nginx routing. The search API itself
(`/api/towers`, plus `/api/elevation` and `/api/config`) is served by
**tower-finder-service**, a separate repo and container that every vhost is
proxied to; the monolith's own copy of that stack was deleted once the proxy went
live, so there is one implementation and one ranking answer.

## Project Structure

```
backend/          Python API (FastAPI)
frontend/         React SPA (Vite)
dashboard/        Admin dashboard (React/Vite)
docs/             Architecture, pipeline, runbook, simulation, arc-display
libs/             Git submodules
  retina-geolocator/   Bistatic passive radar geolocation solver
  retina-tracker/      Multi-target Kalman tracker with anomaly detection
  retina-custody/      Chain-of-custody signing for node detections
  retina-analytics/    Coverage and detection analytics
  retina-simulation/   Synthetic node fleet and world model
```

## Quick Start

### Clone (with submodules)

```bash
git clone --recursive https://github.com/offworldlabs/retina-server.git
cd retina-server

# If already cloned without --recursive:
git submodule update --init --recursive
```

### Backend

```bash
just setup
```

That is the supported path: it initialises the submodules, builds the backend venv
with `uv`, installs all five `libs/` packages editable, seeds `backend/.env` from
the example, applies the database migrations (`backend/data/users.db` does not
exist yet on a fresh clone, and `create_all` no longer builds it outside the test
suite), and installs the frontend dependencies. Install all five even if you only
care about tower search: `retina-simulation` imports the other four, so a partial
install fails at import time rather than at use.

Then either run the whole local stack:

```bash
just up      # uvicorn + synthetic fleet + Vite, with hot reload
just status  # what is alive
just down    # stop it
```

or just the API on its own:

```bash
cd backend && .venv/bin/uvicorn main:app --reload
```

The API runs at `http://localhost:8000`. Interactive docs at `/docs`. The tower
search is not part of this process: run tower-finder-service (its own repo and
container) if you need `/api/towers`, `/api/elevation` or `/api/config` locally.
The live map and the dashboard do not need it.

#### Database migrations

The schema is owned by Alembic (`backend/migrations/`). `create_all` runs only
in the test suite, which sets `RETINA_SCHEMA_SOURCE=create_all`; everywhere else
migrations are applied on every start, by `just up` locally (`just migrate` runs
it on its own) and by `deploy/start.sh` on every container boot. Pulling a branch
that adds a revision therefore needs nothing extra.

To change the schema, edit the models, then from `backend/`:

```bash
uv run alembic revision --autogenerate -m "what changed"
uv run alembic upgrade head
```

Review the generated file before committing. Anything other than `create_table`
must go through `op.batch_alter_table`, because SQLite cannot `ALTER`.
`RETINA_DB_PATH` points Alembic at a scratch file if you want to try a
migration without touching `backend/data/users.db`.

### Frontend

`just setup` already installed the dependencies, and `just up` runs this alongside
the backend. To run it on its own:

```bash
cd frontend && npm run dev
```

Opens at `http://localhost:5173`. API calls are proxied to the backend during
development.

## API

### The v1 node API

The four endpoints under `/v1/nodes` that receiver nodes talk to are a versioned
wire contract, published at [`contracts/nodes-v1.openapi.yaml`](contracts/nodes-v1.openapi.yaml).
The node client and the conformance harness are independent implementations of
it, so it is the one part of this API with consumers holding a pinned version.

That file is **generated, not written**. It comes from the routes and the
Pydantic models, and CI regenerates it and fails when it differs from what is
committed, which is what stops the file being edited to match a change instead of
the change being noticed. Change a node route, then:

```bash
cd backend && RETINA_ENV=dev .venv/bin/python -m scripts.generate_openapi
```

and commit the result alongside. Behaviour a schema cannot carry (whether a
refusal may be retried, and whether retrying can ever help) is annotated onto
the routes as `x-retry` and `x-terminal`, and the vocabulary is defined in the
contract's own description. A breaking change raises `NODE_API_VERSION` in
`backend/routes/nodes.py`.

### `GET /api/towers`, `GET /api/elevation`, `GET|PUT /api/config`

Answered by **tower-finder-service**, not by this backend. nginx proxies all
three to that service on every vhost that answers `/api/` (see
`deploy/nginx/snippets/towers-proxy.conf` and the `TOWER_FINDER` conditional in
`deploy/nginx/nginx.conf.template`); this repo keeps the SPA that calls them and
the routing, and no longer keeps a second implementation of the search, the
ranking engine or the Maprad/FCC clients. Parameters, response shape and the
ranking rules are documented in the tower-finder-service repo, which owns them.

The contract those routes must honour before a vhost is pointed at the service
is asserted by `deploy/tower-contract.sh`, run from CI and from the smoke tests.

## Tech Stack

- **Backend:** Python 3.11+, FastAPI, httpx
- **Frontend:** React 18, Vite, Leaflet
- **Tower search:** tower-finder-service (Maprad.io GraphQL, ACMA RRL, FCC ULS, ISED SMS)
