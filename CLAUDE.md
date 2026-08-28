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
- **The node API contract is generated.** Change a route under `/v1/nodes` or one
  of its models and `contracts/nodes-v1.openapi.yaml` moves with it; regenerate it
  in the same commit or CI fails. See ONBOARDING, "Before you push".
- **This repo is public.** Refer to hosts by SSH alias, never by address, as
  `justfile` already does. No credentials, no droplet addresses, no personal
  accounts in anything committed here.
- **Configuration lives in `backend/.env`**, which is gitignored. Add new keys to
  `backend/.env.example` so the list stays current.
- **Verify on the live map after deploying.** Green tests do not cover the
  compose/env/frontend seams. After any change ships to an environment, open
  that environment's map surface in a real browser — `test-map.retina.fm` for
  the test droplet, `testmap.retina.fm` for staging (Claude: use the Chrome
  browser tools) — and confirm the map renders, aircraft and solves appear,
  and the change behaves as intended before calling the deploy done.
- **An automated browser can show `0 aircraft` on a map that real users see
  working.** The count in the toolbar and the live-stats panel comes from the
  map's own update loop, which a scripted session does not always drive. Judge
  it by `aircraft_on_map` in `/api/test/dashboard` instead, and confirm the
  data path with `/api/radar/data/aircraft.json` from the page's own origin: a
  200 carrying aircraft means the server and the seam are fine and only the
  rendering was not exercised. Nodes, basemap and controls do render, so a
  blank map is still worth chasing — it is the aircraft count alone that lies.
