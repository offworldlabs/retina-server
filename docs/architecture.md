# Architecture

A system overview for developers. For setup see [`../ONBOARDING.md`](../ONBOARDING.md);
for the detection internals see [`pipeline.md`](pipeline.md).

## One backend, several surfaces

A single FastAPI app (`backend/`) serves every user-facing surface. The public
ones share one hostname, `app.retina.fm` (`staging-app`, `test-app`), because
the session cookie is host-only and a login has to cover all of them:

All of them are one SPA, the console (`dashboard/`), served at the root; the old
`/dash/…` and `/data/…` addresses redirect into it with their query strings.

- **map** (`/map`, where `/` opens) — the live map, the console's front page.
  Every deployed environment shows real nodes only here (the default is
  resolved client-side in `dashboard/src/pages/map/utils/domains.ts`; the
  laptop keeps both fleets). The synthetic simulation fleet has its own page,
  `/sim`, which is populated only where a fleet runs: the test droplet, and a
  local stack. Production and staging run none.
- **console pages** — node ownership, the node claim page, MLAT verification, metrics.
  A session is required for all of it bar the routes listed in
  `dashboard/src/utils/publicRoutes.ts`, which render to anyone and are backed
  only by endpoints that already publish. A caller with no session gets a nav
  holding those routes alone, so nothing on screen leads to the login card.
- **data explorer** (`/data`) — the public detection archive browser, one of the
  console's public routes. Old links to the standalone page it replaced keep
  their filters through the redirect. It reads `/api/data/archive`, which is unauthenticated
  and drops private nodes for every caller, so a signed-in owner sees their own
  private nodes' files only once an authenticated listing exists; the public
  route must not grow one.
- **admin** (`admin.retina.fm`) — the dashboard bundle again, built at a root and
  serving the admin route table, which `dashboard/src/utils/surface.ts` selects
  from the hostname. It keeps a name of its own because a Cloudflare Access
  application can only be scoped to one.
- **Illuminator search** — not a surface of this repo. tower-finder-service owns
  both the API and the UI, and serves `towers.retina.fm` from its own edge. The
  vhosts here proxy `/api/towers`, `/api/elevation` and `/api/config` to it.

## Data flow

```
receiver nodes ──TCP frames──▶ tcp_handler ──▶ frame_queue
                                                   │
                                          frame_processor (N workers)
                                                   │
                              ┌────────────────────┼─────────────────────┐
                              ▼                     ▼                     ▼
                    retina-tracker        node_associator          single-node
                    (Kalman + GNN)     (multi-node candidates)    bistatic arc
                              │                     │
                              ▼                     ▼
                                            solver_queue ──▶ solver workers
                                                              (retina-geolocator,
                                                               LM multinode solve)
                                                   │
                                                   ▼
                              state (in-memory): tracks, aircraft, arcs
                                                   │
                              aircraft_flush_task (~2 Hz) builds aircraft JSON
                                                   │
                    ┌──────────────────────────────┼───────────────────────────┐
                    ▼                               ▼                            ▼
              /ws/aircraft                 /ws/aircraft/live            /ws/aircraft/owner
              (all nodes)                  (real nodes only)            (one owner's nodes)
```

The detection pipeline (tracker → geolocator) is documented in detail in
[`pipeline.md`](pipeline.md). Bistatic uncertainty arcs and how they're rendered
are in [`arc-display.md`](arc-display.md).

## Backend components

- **`routes/`** — HTTP + WebSocket endpoints (radar, streaming, auth, admin,
  analytics, nodes, test, output). No tower routes: nginx sends those to
  tower-finder-service.
- **`services/frame_processor.py`** — frame ingest: turns detection frames into
  per-node tracks. Split satellites: `aircraft_feed.py` (combined aircraft JSON
  assembly), `track_gates.py` (per-track gates, dead-reckoning, anomaly flags,
  single-node arc builder), `feed_gc.py` (stale-store GC), `feed_helpers.py`
  (dedup, history, arc-motion velocity), `geo.py` (one home for the spherical
  geometry and the beam/range semantics every gate shares).  Node beam/range
  geometry (`beam_azimuth_deg`, `beam_width_deg`, `max_range_km`,
  `max_bistatic_range_km`) flows from node registration into the per-node
  pipelines, the arc builder, and inter-node association — one contract.
- **`services/node_refs.py`** — publication identities and payload rewriting.
  Registered nodes use their stored refs, mirrored nodes preserve upstream
  refs, synthetic nodes may retain synthetic identities, and unresolved real
  nodes are withheld. Internal `node_id` fields become public `node_ref`
  fields after privacy and feed filtering. The inverse, `ref_to_id_map`,
  is served from one admin-only route (`GET /api/admin/node-refs`) and exists
  so the dashboard can name a node to an operator and link to its own site,
  which is `<node_id>.retnode.com`.
- **`services/tasks/`** — background async tasks: `aircraft_flush` (broadcast),
  `feed_gc` (stale-store GC on its own 5 s timer, deliberately not tied to the
  feed build), `solver` workers, `analytics_refresh`, archive lifecycle,
  snapshots, `health_monitor` + `heartbeat` (see [`alerting.md`](alerting.md)).
- **`core/state.py`** — the in-memory world: connected nodes, tracks, aircraft,
  arc buffers, WebSocket client sets, latest JSON payloads.
- **`core/users.py` + `core/auth.py`** — fastapi-users (cookie JWT, sign-in by
  emailed link) plus domain auth: node ownership and emailed links (SQLite).

## The algorithm libraries (submodules)

The math lives in separate repos under `libs/` so it can be versioned and reused:

- **retina-geolocator** — bistatic delay/Doppler position solver. Single-node
  produces an ellipse arc (a locus, not a point); multi-node (n≥2) runs an LM
  least-squares solve for a position, with an altitude sweep for n≥3.
- **retina-tracker** — Kalman multi-target tracker + anomaly detection.
- **retina-simulation** — synthetic fleet generator (powers the console's
  `/sim` surface + CI), on the deployments that set `SYNTHETIC_FLEET_ENABLED`.
  Runtime-tunable via `PUT /api/simulation/config` (target-class fractions,
  aircraft counts), which the fleet polls every 5 s; fleet scale itself comes
  from the deployment env (`FLEET_*` in the compose files).
- **retina-custody** — custody protocol.
- **retina-analytics** — node trust/reputation, inter-node track association
  (pairing + top-down claiming), and per-node empirical coverage / learned
  FOV (see [`pipeline.md`](pipeline.md) §4 and §7).

## Feature gates

The multi-node stack's newer stages ship behind env flags (set in the
gitignored `backend/.env`; unset = the safe default):

| Env | Values | Default | Staging | Gates |
|-----|--------|---------|---------|-------|
| `SOLVER_CONSENSUS_MODE` | `off/shadow/active` | `off` | `active` | n≥3 consensus-refine hypothesis stage |
| `ASSOC_CLAIM_MODE` | `off/shadow/active` | `off` | `active` | top-down tracklet claiming from global tracks |
| `FOV_MODE` | `off/shadow/active` | `off` | `active` | learned empirical FOV as association grid + solver beam gate |
| `ADSB_SEED_MODE` | `off/shadow/active` | `off` | `active` | ADS-B-seeded detection assignment: verified lit tracklets leave dark pairing, re-emitted as `mn-adsb-*` seeded solves |
| `KNOWN_LANE_MODE` | `off/shadow/binding` | `binding` | `binding` | identity-first known-target claiming: per-frame detections bound to live ADS-B hexes (`state.known_claims`) leave the dark pool before the tracker/association ever see them |
| `TRACK_SMOOTHER` | `kf/ewma/off` | `kf` | `kf` | display smoothing for multinode tracks (`ewma` is the rollback) |
| `REPUTATION_PENALTY_SCALE` | float ≥ 0 | `0` | `0` | multiplier on every node-reputation penalty; `0` means no node can be blocked, `1` is the historical behaviour (temporary — see [`runbook.md`](runbook.md)) |

`shadow` computes and counts a stage's verdicts (exposed in
`/api/test/solver-stats`) without letting them bind — the standard soak step
before flipping `active`. Production currently sets none of the mode flags
(all `off`). `KNOWN_LANE_MODE` differs from its siblings on both axes by
design: its acting value is named `binding` (a claim *binds* a detection to a
transponder identity), and it is the one flag whose default is the acting
value, set in no environment's `.env` — it cleared its shadow soak, and three
consumers now depend on the registry it fills (the known-lane solver, the
per-node trust residuals, and the feed's `adsb_single_node` display section).

## State & storage

- **In-memory first.** Tracks, aircraft, node sessions live in `core.state`.
  A restart drops them.
- **Snapshots.** State is serialized to disk every 60s and restored on boot
  (trust scores, reputations, accuracy samples, node identities).
- **SQLite** (`data/users.db`) — users, and node claims: who owns each node and
  the address it was claimed with.
- **R2 (Cloudflare).** Archived coverage/track Parquet is offloaded to the
  `retina-server-archive` bucket and pruned locally (see the runbook).

## Auth model

Cookie-based JWT issued against a sign-in link mailed to the address, shared
across surfaces on the same origin. Administrators arrive instead through Cloudflare Access: the origin
verifies the `Cf-Access-Jwt-Assertion` itself against the team's published keys,
with `aud` pinned per environment to `CF_ACCESS_AUD`, and the verified email is
the identity (`backend/core/access_identity.py`). Enforcement is backend-side
because every vhost proxies `/api/` to the same app, so gating one hostname at
the edge would protect that hostname's HTML and nothing else; it is also why
`api.retina.fm`, the fleet's ingest hostname, carries no Access application.
`AUTH_ALLOW_ANONYMOUS_ADMIN=1` still grants the anonymous-admin bypass,
independent of `RETINA_ENV`, but only `docker-compose.local.yml` sets it. Node
ownership maps
`node_id → user_id`; the `/ws/aircraft/owner` feed and dashboard use it to scope
data to a user's own nodes.

## Deploy

`.github/workflows/ci.yml`: push to `main` → build/test → deploy staging →
staging smoke + E2E → deploy production → prod smoke + E2E. The three staging
steps live in `staging-deploy-verify.yml` and are called as a single job, so one
run holds the environment until its own verification has finished. Deploy is an
SSH `git reset --hard origin/main` + `docker compose up -d --build`, gated by a
free-disk pre-flight. Operational detail is in [`runbook.md`](runbook.md).
