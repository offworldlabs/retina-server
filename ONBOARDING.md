# retina-server — developer onboarding

Welcome. This repo powers RETINA, a passive-radar system: a network of receiver
nodes detect aircraft by listening to reflections of broadcast transmitters
(bistatic radar), and the backend turns those detections into tracks and live
positions shown on a web map. It started life as "Tower Finder", a tool to find
suitable broadcast illuminators near a receiver; that feature now lives entirely
in tower-finder-service, both its API and its UI.

Read this top-to-bottom once; it should get you from a fresh clone to running
the whole thing locally and understanding how the pieces fit. For deeper dives,
see [`docs/architecture.md`](docs/architecture.md) and the other docs linked at
the end.

## The big picture

One FastAPI backend serves several React front-ends. `app.retina.fm`
(`staging-app`, `test-app`) carries the public ones on one hostname, because the
session cookie is host-only and a login has to cover all of them:

| Surface | What it is |
| --- | --- |
| **map** (`/map`, where `/` opens) | Live aircraft map, the console's front page. Every deployed environment shows real radar nodes only here; the synthetic fleet, where one runs, is on the admin console at `/sim`. The `/map` default is chosen by hostname in `dashboard/src/utils/domains.ts`, and only the laptop keeps both fleets on it. |
| **console** (the rest of `/`) | Node ownership, the node claim page, MLAT verification, metrics. Auth required, bar the public pages such as the map and the detection archive browser at `/data`. |
| **admin** (`admin.retina.fm`) | The same dashboard bundle with the admin route table, on a hostname of its own so a Cloudflare Access application can gate it. |

Illuminator search is deliberately absent from that table: **tower-finder-service**
(separate repo and container) owns the API and the UI both, and serves
`towers.retina.fm` from its own edge. Our vhosts only proxy `/api/towers`,
`/api/elevation`, `/api/config` and `/api/geocode` to it.

Receiver nodes connect over TCP and stream detection frames. The pipeline
(tracker → geolocator) turns frames into aircraft positions, broadcast to the
map over WebSocket. See [`docs/pipeline.md`](docs/pipeline.md).

## Repo layout

```
backend/      FastAPI API, TCP frame ingest, detection pipeline, background tasks
dashboard/    The console, live map included (React, Vite, Leaflet)
packages/shared/  Code the console shares, imported as @retina/shared
e2e/          Playwright suite run after each deploy; a failure on production rolls it back
libs/         Git submodules (the algorithm libraries — see below)
docs/         Architecture, pipeline, runbook, alerting, simulation, arc-display
```

### Submodules (`libs/`) — the "other repos"

These are separate GitHub repos under `offworldlabs/`, vendored as submodules so
the algorithms can be reused and versioned independently:

| Submodule | Purpose |
| --- | --- |
| `retina-geolocator` | Bistatic delay/Doppler position solver (single- and multi-node, LM least-squares). |
| `retina-tracker` | Multi-target Kalman tracker + anomaly detection. |
| `retina-simulation` | Fleet simulator that generates synthetic radar frames for the admin console's `/sim` map and CI. |
| `retina-custody` | Custody-protocol library. |
| `retina-analytics` | Node trust/reputation analysis. |

`retina-analytics` is pinned to a commit on its `pin/tower-finder` branch rather
than `main`. That branch carries no CI and no ruff config, so a PR against it
reports no checks at all; verify it from this repo's suite instead.

## Local setup

Clone with submodules, then set up backend and front-ends.

```bash
git clone --recursive https://github.com/offworldlabs/retina-server.git
cd retina-server
# already cloned without --recursive?
git submodule update --init --recursive
```

### Backend

```bash
cd backend
uv sync && source .venv/bin/activate   # the lock, dev tools and all five libs, editable
pre-commit install --install-hooks   # the lint gate on every commit; see "Before you push"
cp .env.example .env          # fill in what you need (see below)
RETINA_ENV=dev AUTH_ALLOW_ANONYMOUS_ADMIN=1 SYNTHETIC_FLEET_ENABLED=1 uvicorn main:app --reload
```

API at `http://localhost:8000`, and its reference at `/`. That page and
`/openapi.json` list only the public routes; with the bypass below, the whole
schema is at `/api/admin/openapi.json`.

`AUTH_ALLOW_ANONYMOUS_ADMIN=1` grants the anonymous-admin bypass, so you need
no identity provider locally: with Cloudflare Access unconfigured, you're
treated as an admin. `SYNTHETIC_FLEET_ENABLED=1` mounts the simulation ingest routes the
fleet pushes through, without which `/api/sim/adsb/push` answers 404.
`RETINA_ENV=dev` (or `test`) separately relaxes the boot-time secret checks, so a
local run needs no real `JWT_SECRET`; a deployed environment requires one.

All three go on the command line rather than in `.env`, because `main.py` loads
the dotenv file after the modules that read them. `just up` passes them for you.

Dependencies are declared in `backend/pyproject.toml` and pinned, transitive ones
included, by `backend/uv.lock`. The images and CI install from it with the uv
the `Dockerfile` pins as `UV_VERSION`, and the lock is written with that uv too:
`uv tool run uv@<UV_VERSION>` runs it beside your own. From `backend/`, change a
dependency with `uv tool run uv@<UV_VERSION> add` (or `remove`), or edit
`pyproject.toml` and run `uv tool run uv@<UV_VERSION> lock`, and commit the two
together. A submodule bump that changes a lib's own dependencies needs the lock
as well. CI syncs with `--locked`, so a lock left behind fails the run. After a
pull that moves the lock, `uv sync` again.

### The console

The console is a workspace of one npm package at the repo root: one lockfile,
one `npm ci`, and the toolchain (Vite, Vitest, TypeScript, ESLint) declared once
in the root `package.json`. Install at the root, then address it with `-w`:

```bash
npm ci                    # once, at the repo root
npm run dev -w dashboard
```

Shared code lives in `packages/shared`, imported as `@retina/shared`; it is a
workspace with its own lint, typecheck and tests. Reach for its
`request()` rather than a raw `fetch`: it carries the timeout, the JSON
conventions and the typed errors every surface wants. `useCurrentUser()` sits on
top of it and resolves who the caller is, retries included.

The lockfile is written by npm 10, the version CI and the image run (Node 20).
A local npm 11 writes one that npm 10 rejects as incomplete, so after changing a
dependency regenerate it with the same npm (as your own user, so the file it
writes stays yours on a Linux host):

```bash
docker run --rm -v "$PWD":/w -w /w --user "$(id -u):$(id -g)" -e npm_config_cache=/tmp/.npm \
  node:20-alpine npm install --package-lock-only
npm ci
```

The console is at `http://localhost:5174` (or `http://app.localhost:5174/`) and
opens on the live map; `/api` and `/ws` are proxied to the backend on `:8000`,
and `?mode=admin` selects the admin console. The hostname selects `/map`'s
default feed (see `dashboard/src/utils/domains.ts`); a local hostname shows
both real and synthetic nodes.

There's a backend-free map sandbox at `/test-radar` (one node, one aircraft,
one ellipse) for working on map rendering without the pipeline. Only dev builds
carry it.

### Full stack in Docker

To run the built image exactly as the droplets do (nginx rendered from the
shared template, plain HTTP), overlay the laptop compose file on the base:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

Serves `http://app.localhost:8080` (the console, opening on the live map of the
synthetic fleet), `http://api.localhost:8080`, and towers/admin on the same
port — the endpoint list and the reasoning live in `docker-compose.local.yml`'s
header. Always pass `--build`: the console bundle and backend are baked into the
image, so a plain `up` silently reuses the previous build.

### See real data without running the pipeline

The simulation fleet (`retina-simulation`) feeds the admin console's `/sim`
map. Production and staging run no fleet, so `test-admin.retina.fm/sim`, behind
Cloudflare Access, is the deployed surface that shows one. To drive a local
backend with synthetic frames, see [`docs/simulation.md`](docs/simulation.md).

### Working in a git worktree

A fresh worktree has empty `libs/` directories, no `node_modules` and no venv of
its own. Build them the way CI does, or pytest fails at conftest import on a
missing `sqlalchemy`. The submodules come first: `uv sync` builds the libs from
them.

```bash
git submodule update --init
npm ci
cd backend && uv sync
```

## Running tests

```bash
# backend
cd backend && RETINA_ENV=test COVERAGE_CORE=sysmon pytest

# every workspace; -w dashboard (or -w packages/shared, -w e2e) for one
npm run test --workspaces --if-present && npm run typecheck --workspaces && npm run lint --workspaces --if-present

# the browser suite, against staging (local and prod are the other two targets)
npm run test:e2e:staging -w e2e
```

Backend coverage gate is 55%. Async tests need `pytest-asyncio` (in the
`dev` group, which `uv sync` installs) — without it they silently skip.

Trust pytest's **exit status**, not the tail of its output. The warnings block
and the coverage footer both print after the summary line, so piping the run
into `tail` loses the `N passed` and a passing-looking tail proves nothing.

Two suites can now run at once, from two worktrees, without interfering. They
could not before: `backend/main.py` binds `RADAR_TCP_PORT` (default `3012`) in
the app lifespan, and `tests/test_storage.py` wrote into the shared
`backend/coverage_data/archive` and deleted its contents afterwards, so a second
run got bogus route and health errors from the port and the occasional
`FileNotFoundError` from the archive. `tests/conftest.py` now asks the kernel
for an ephemeral port, and those tests use `tmp_path`. The database path has
been per-pid for longer, for the same reason.

This is also what lets CI run the suite under `pytest-xdist`; see the comment on
the pytest step in `.github/workflows/ci.yml` for why it passes
`--dist worksteal` and why the flags are not in `addopts`.

CI splits the suite across three runners on top of that, with `pytest-split`
cutting the collected tests into contiguous chunks of equal recorded duration.
A shard measures only its own third, so each overrides the 55% gate away and
uploads its coverage data; the `backend-coverage` job combines the three and
applies the threshold once. A local `pytest` is untouched by all of this and
still enforces the gate itself.

`backend/.test_durations` only decides where the two boundaries fall, so a stale
one costs balance and never correctness. Regenerate it when the shards drift
apart, from a serial run:

```bash
cd backend && pytest tests/ -m "not external" --no-cov --store-durations
```

Not under `-n`: each xdist worker records only the tests that landed on it, and
the file it writes covers a fraction of the suite.

`COVERAGE_CORE=sysmon` above is not decoration, and CI sets it too. Without it,
coverage measures through `sys.settrace`, which is per execution context, so
every line after an `await session.…` in a greenlet-backed path counts as unrun:
`[tool.coverage.run]` in `backend/pyproject.toml` sets no `concurrency` to
compensate. The async route modules are what this hits. sysmon measures through
the PEP 669 interpreter-wide hooks instead, has no such blind spot, and is more
than twice as fast (in CI, 225s to 95s). Drop the variable and you get a slower
run and a lower figure than CI reports for the same commit.

`concurrency = ["thread", "greenlet"]` is the other way to close the gap, and on
the module it was checked against it recovers the same lines, but it is more than
ten times slower than sysmon and there is no longer a reason to reach for it.

### Before you push

The lint gate is pre-commit, not the two ruff commands. Once installed (see
Local setup) it runs on the staged files at every commit, in every worktree:
hooks live in the clone's shared `.git/hooks`. The hook records the absolute
path of the venv it was installed from, so reinstall it if that venv moves.

CI runs it over every file, and so should you before pushing, since a commit
made with `--no-verify` or from somewhere without the hook skipped it:

```bash
backend/.venv/bin/pre-commit run --all-files
```

It runs `ruff-check`, `ruff-format`, actionlint over the workflows, a dead-code
check (vulture) and `ruff-config` twice, once per copy of the shared standard in
this repo. A change can pass `ruff check` and `ruff format` by hand and still
fail CI on dead code.

Touching a node route or one of its models also moves the node API's wire
contract, which is generated rather than written. So does changing a
configuration bound: the schema published for `config` is built from the
validator's own tables, so `backend/services/node_config.py` moves the contract
with no route touched. Regenerate it in the same commit, or CI fails on a file
you never edited:

```bash
cd backend && RETINA_ENV=dev .venv/bin/python -m scripts.generate_openapi
```

That gate is what makes the generated contract trustworthy: the committed file
cannot be edited by hand to match a change, because the next run regenerates it
and notices.

Two traps in that command:

- **`--all-files` does not mean all files.** pre-commit enumerates through
  `git ls-files`, so untracked files are skipped silently. `git add` new modules
  and tests first, or a clean run tells you nothing about them.
- **Vulture flags module-level singletons** nothing imports yet, so a module built
  ahead of its callers fails on the instance rather than the class.
  `backend/vulture_whitelist.py` is for names referenced dynamically, not for
  future wiring: omit the instance until something uses it.

## How code ships

CI runs on every PR, on push to `main`, and on demand through
`workflow_dispatch` (`.github/workflows/ci.yml`):

1. Any PR, whatever its base: `backend-tests` (three shards) and
   `backend-coverage` behind them, `lint`, `web-build` (once per
   workspace, with the dashboard's tests apart from its other checks),
   `docker-build`, `env-parity`, `tower-service-contract` (a probe of
   production's tower-finder-service) and `playwright-image`, plus an
   automated review. `ci-ok` needs every one of those jobs bar the review and
   fails unless all of them passed, so it alone says whether a PR passed CI.
2. Merge to `main` → deploy to **staging** → staging smoke + Playwright E2E → deploy to **production** → prod smoke + Playwright E2E.
   A merge that changes nothing the droplets serve skips that chain, which means
   markdown, and Python whose syntax tree has not moved: a reworded comment or a
   `ruff format` pass ships nothing. `deploy/deploy-scope.py` holds the rules and
   the exceptions, and its tests hold the verdicts.
   The staging third of it is a called workflow,
   `.github/workflows/staging-deploy-verify.yml`, invoked from one `Staging`
   job so that job's concurrency group is held across the deploy and both
   suites. Adding a staging step means editing that file, not `ci.yml`.
   Both deploys take a rollback point first and roll themselves back when
   they fail after it; the runbook's Environments section has the shape.

So merging to `main` deploys to production automatically. Work on a feature
branch, open a PR, get it green, then merge.

## Things that will bite you

- **A cancelled `Staging` job on a burst of merges is expected, not a fault.**
  Only one run may sit pending on the `staging-deploy` group, so when a third
  merge arrives while one run holds staging and another is queued, the queued
  one is cancelled. `main` is linear, so the run that replaces it deploys a
  superset of what was dropped. What it does mean is that the cancelled
  commit's own run never reaches production: the following run carries it.
  The last merge in a burst has no successor, so check it landed.

- **The CARTO basemap key lives on the droplet, not in this repo.** Every
  deploy appends `/root/.secrets/carto.env` to `./.env` after copying
  `deploy/env.<env>.example`, and `docker-compose.yml` interpolates it into the
  console's `VITE_CARTO_API_KEY` build arg. The file is one line,
  `CARTO_API_KEY=<key>`; create it on a new droplet with
  `install -d -m 700 /root/.secrets` and a `printf` into
  `/root/.secrets/carto.env` (`chmod 600`). It is deliberately not in
  `backend/.env` — Compose reads build args from `./.env` alone, never from an
  `env_file` — and deliberately not committed, because this repo is public and
  a key in its history outlives every rotation. A host without the file still
  deploys; its maps just come back stamped "API KEY REQUIRED".
- **All backend state is in-memory.** A restart loses connected nodes and live
  tracks; state is snapshotted to disk every 60s and restored on boot. Don't
  assume persistence.
- **Submodules.** After pulling, run `git submodule update --init --recursive`
  if `libs/` looks stale or imports fail.
- **The map opens on localhost too.** Each environment has two consoles, and
  the hostname chooses between them. The app (`app`, `staging-app`,
  `test-app`) is the public view of the network, with the real nodes at
  `/map`. The admin console (`admin`, `staging-admin`, `test-admin`, behind
  Cloudflare Access) holds the simulated fleet at `/sim` and the page that
  tunes it at `/sim/physics`, whose save needs an administrator. Both routes
  exist only where the server sets `SYNTHETIC_FLEET_ENABLED` (the test droplet
  and the laptop; staging and production run no simulator), as `/api/auth/me`
  reports. On the dev server that is `/sim?mode=admin`. `map`, `staging-map`
  and the other retired names are Cloudflare redirects into the app consoles;
  `testmap` and `test-testmap` are retired outright, with no redirect. `/map`
  still takes its feed from the hostname: every deployed environment (`app`,
  `staging-app`, `test-app`) is real-only there, and a local hostname retains
  both kinds of node. Tower search has its own SPA in
  tower-finder-service; the laptop overlay sets `TOWER_FINDER_ENABLED=false`,
  and this backend no longer implements `/api/towers`.
- **Config vs runtime config.** `backend/config/` is image-only (baked into the
  Docker image); runtime-editable overrides live under `data/runtime/`. See the
  runbook for the volume-shadowing gotcha.
- **A branch opened before #187 runs no CI at all.** The workflow used to trigger
  on `branches: [main]`, which matches a PR's *base*, so a PR stacked on a feature
  branch got no tests, no lint and no build while the lone green automated-review
  tick made the page read as passing. Stacked PRs opened since run the full matrix,
  but an older branch keeps the old workflow until it is rebased.
- **The compose service is `server`, not `tower-finder`.** Naming a service that
  does not exist fails with `no such service` on *stderr*, so a `grep` over the output
  matches nothing and reads as a clean "no errors in the logs". Run `docker compose ps
  --services` first and trust it over a remembered name.
- **A new per-environment key needs an `env-parity` entry** or CI fails.
- **Editing a PR's title or body starts a CI run that skips every gate**, and
  `gh pr checks` then lists those skips beside the real results or in place
  of them. Read the check named exactly `ci-ok` instead. The edit's run
  reports as `ci-ok (title or body edit)`, which is never a verdict, so the
  `ci-ok` the last push or retarget left still stands.

## Where to go next

- [`docs/architecture.md`](docs/architecture.md) — system overview, data flow, surfaces, storage.
- [`docs/pipeline.md`](docs/pipeline.md) — detection → tracker → geolocator → aircraft JSON.
- [`docs/arc-display.md`](docs/arc-display.md) — how bistatic uncertainty arcs are drawn.
- [`docs/runbook.md`](docs/runbook.md) — production operations, server access, incident response.
- [`docs/alerting.md`](docs/alerting.md) — monitoring, alerts and the outside-in probes.
- [`docs/simulation.md`](docs/simulation.md) — running the fleet simulator.
