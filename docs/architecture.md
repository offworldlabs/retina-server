# Architecture

A system overview for developers. For setup see [`../ONBOARDING.md`](../ONBOARDING.md);
for the detection internals see [`pipeline.md`](pipeline.md).

## One backend, several surfaces

A single FastAPI app (`backend/`) serves every user-facing surface. The public
ones share one hostname, `app.retina.fm` (`staging-app`, `test-app`), because
the session cookie is host-only and a login has to cover all of them:

All of them are one SPA, the console (`dashboard/`), served at the root.

- **map** (`/map`, where `/` opens) — the live map, the console's front page.
  Every deployed environment shows real nodes only here (the default is
  resolved client-side in `dashboard/src/utils/domains.ts`; the
  laptop keeps both fleets). The synthetic simulation fleet is on the admin
  console at `/sim`, which exists only where a fleet runs: the test droplet,
  and a local stack. Production and staging run none.
  A `#t=<ISO time>` link opens the map in archive mode, which replays
  `/api/data/tracks` through the same feed hook. There `G` shows the
  archived ADS-B traffic of the replayed instant (`/api/data/adsb`).
- **console pages** — your nodes and their data, the node claim page with
  resend, the account page with name, sessions, address change and deletion,
  metrics. A session is required for all of it bar the routes listed in
  `dashboard/src/utils/publicRoutes.ts`, which render to anyone and are backed
  only by endpoints that already publish. A caller with no session gets a nav
  holding those routes alone, so nothing on screen leads to the login card.
- **data explorer** (`/data`) — the public detection archive browser, one of the
  console's public routes. Old links to the standalone page it replaced keep
  their filters through the redirect. It reads `/api/data/archive`, which is
  unauthenticated, names each file by its node's `node_ref` (the store is keyed
  on `node_id` and the route translates both ways) and drops private nodes for
  every caller, their owner included. A signed-in owner reads their own nodes'
  files, private ones too, from `/api/auth/me/archive`, each from when it became
  theirs; no public route answers differently by session.
- **tracks archive** — the published solves, read back for the map's archive
  mode. `/api/data/tracks` takes a window of at most 60 minutes, an optional
  bbox and a fleet (`real` by default, `synthetic`, or `all` to add mixed and
  no-contributor solves), and returns up to 50,000 rows by frame time, with
  `truncated` past that. Contributors go out as `node_refs`, the refs the live
  feed published them under at the time, or the ones they resolve to now where
  the row stored none; a solve with a private or probation node, or a real
  node with no published identity, is dropped, not redacted.
  `/api/data/tracks/availability` counts rows per hour over at most 92 days
  from Parquet footers alone; the counts are network-wide, so they can include
  rows the tracks route withholds. Both spend the archive read budget below.
  Data explorer file rows ("Show on map", at the file's `first_frame_ms`) and
  published solves in the admin solve history (`hex=` selects the solve) link to
  it; synthetic-fleet entries, private nodes and unpublished solves get none.

  Once a UTC day is complete (30 minutes after it ends), an hourly sweep
  rewrites that day's raw parts into one `compact.parquet`, beside a
  `compact.json` that carries the same manifest plus the file's size and
  sha256. The compact has one row group per UTC hour and node kind, in time
  order. Readers skip the raw parts it lists and still read any part flushed
  after it was built. A part that cannot be read stays raw. The compact is
  written to a tmp file, read back and checked, then renamed into place. No
  crash makes a raw part look compacted while its rows are not in a readable
  compact. The sweep runs on every host, with an object store or not. Availability
  answers from Parquet footers and sidecars. A part that straddles an hour
  counts in the hours of its own rows once compacted.

  The archive lifecycle uploads the compact as soon as it is written and
  uploads it again after a rewrite. It deletes the local compact once its day
  ended more than `TRACKS_COMPACT_RETENTION_DAYS` ago (365 by default, 0 keeps
  it forever), it is uploaded, and its day has no raw part left on disk.
  `compact.json` and the compact's `.uploaded` sentinel are never deleted, so
  a superseded raw part left over after the compact is gone can still be shown
  safe and deleted. A raw part is deleted at `ARCHIVE_RETENTION_DAYS` only if
  it is uploaded, listed by the compact, and that compact is safe (on disk, or
  uploaded). A host without an object store compacts but uploads and deletes
  nothing, so the raw parts stay beside the compacts.

  A day with no usable compact on disk is read back from the object store, on
  a host that has one and once the day is complete. With no readable sidecar,
  the reader uses the raw parts on disk, and goes to the store only when there
  are none. With a sidecar and every part it lists still on disk, or when the
  day cannot be fetched, it reads the raw parts and fetches nothing. Otherwise
  it reads the compact fetched from the store plus the raw parts the sidecar
  does not list. Availability follows the same rules, from the sidecar and
  without fetching. The compact is fetched once per day into
  `coverage_data/cold_cache/tracks/YYYY-MM-DD.parquet`, checked against the
  sidecar's size and sha256, and reused until evicted. The cache is held to
  `TRACKS_COLD_CACHE_MAX_BYTES` (2 GiB default, 0 keeps nothing between
  requests), dropping the least recently used day first. A day with no
  sidecar whose compact the store does not have is remembered as missing for
  10 minutes (`TRACKS_COLD_MISS_TTL_S`) and has no rows. A compact that cannot be fetched or fails
  validation while its sidecar promises it is not fetched again for 30
  seconds, so a burst of requests fails fast; a changed sidecar is fetched at
  once. A request that waits more than 20 seconds behind another request's
  fetch of the same day gives up instead of queuing. `/api/data/tracks`
  answers 503 ("The archived day YYYY-MM-DD could not be loaded from cold
  storage.") in those three cases. Both endpoints, and `/api/data/adsb`, run
  their store work on one pool of 4 threads kept apart from the server's
  shared pool, so slow cold reads never hold the shared pool; a cold day of
  either archive does hold one of the 4 while it loads. In
  `/api/data/tracks/availability` an hour of a day read this way has
  `cold: true`. The map shows a "Loading ... from cold
  storage" notice while a window touching a cold hour loads, and a "Cold
  storage" row on the availability strip. Days deleted locally before
  compaction existed are compacted from the object store once, by the command
  in the runbook, "Compacting days that live only in the object store".
- **detections archive** — the detections behind the map's arcs in archive mode.
  `/api/data/detections` takes a window of at most 10 minutes, `to` exclusive,
  an optional bbox (a detection is inside when its receiver or its transmitter
  is) and a fleet like the tracks route, and returns up to 100,000 rows by
  frame time, with `truncated` past that. It reads the same Parquet files as
  the Data Explorer, published the same way: private nodes' rows are dropped,
  node ids become `node_ref`s, and a real node with no published identity is
  dropped. Only files stamped with the published receiver position are read,
  and the next day's partition too, for files flushed after midnight. `local`
  is false when an object store is configured and the lifecycle may already
  have deleted some of the files the window can be in (a node clock running
  ahead puts its first frames in a file flushed before it opens); the map then
  says some arcs may be missing. `pending` is true while the hourly archive
  write may not have reached all of the window: until the last write this
  process recorded finished at least ten minutes after the window's end, and
  at most an hour and ten minutes after that end. The map then says the newest
  hour's arcs come with that write, and asks for the window again once the
  archive can hold it. It keeps its own bounded cache.
- **ADS-B archive** — a snapshot of the server's ADS-B picture every 10 s,
  keeping a fix only while it is fresh enough for the claim lane (45 s). It
  stores each node's own receiver, the simulator's own fleet and, where
  `ADSB_FALLBACK_ENABLED=1`, ADSBHub traffic from adsb.retina.fm; OpenSky and
  adsb.lol truth and the simulator's adsb.lol relay are never stored, and
  neither is a fix whose reporter is private or unidentified when the snapshot
  is taken. Every public node that hears a real aircraft keeps a row of its
  own fix, so a node made private later takes its own fixes out of what is
  served and nothing else: the aircraft stays wherever another public node
  heard it, from that node's fixes. Rows grow with the number of public nodes
  that hear each aircraft: a seeded day with six public nodes within 45 km of
  one point wrote 3.8 times the rows that one node row per aircraft would,
  150 MB instead of 49 MB. Snapshots are buffered and written once a minute as
  one raw part under `coverage_data/adsb/`. The store is compacted, kept and
  read back like tracks (above): each complete UTC day becomes one
  `compact.parquet` with a row group per hour and world, in time order, beside
  its `compact.json`. The compact keeps every row as written, its attribution
  and receive time included, so a day answers the same from its raw parts,
  its compact or the object store, and a node withheld later is dropped from
  all three alike. The lifecycle uploads the compact and keeps it locally for
  `ADSB_COMPACT_RETENTION_DAYS` (365 by default, 0 keeps it forever); a raw
  part is uploaded after 1 day and deleted at `ARCHIVE_RETENTION_DAYS` (3
  days) only once it is uploaded, listed by the compact, and that compact is
  safe (on disk, or uploaded). On a host with an object store, the compact
  retention is what bounds the local disk the archive takes. A day whose
  compact is only in the object store is fetched into
  `coverage_data/cold_cache/adsb/`, held to `ADSB_COLD_CACHE_MAX_BYTES` (1 GiB
  default), a day the store does not have is remembered as missing for
  `ADSB_COLD_MISS_TTL_S` (10 minutes), and `/api/data/adsb` answers 503 when it cannot be loaded, as the
  tracks route does. An ADSBHub
  fix is kept only inside a node's published range disc (the 150 km poll margin
  less the fuzz, site radius and a measuring slack: 146.5 km by default), since
  the polled region is centred on
  true node positions. `/api/data/adsb` takes a window of at most 60
  minutes, an optional bbox and a fleet (`real` by default, `synthetic` or
  `all`), and returns up to 100,000 rows by time then hex, with `truncated`
  past that and one fix per aircraft per snapshot, the freshest that may be
  published, with when it was received (`seen_ms`, to the second, never after
  the snapshot). A node's own fix is served only while that node is public
  today (not private and, for a real node, with a published identity), and an
  ADSBHub fix while one of the nodes whose range holds it is; anything else is
  dropped before that pick. The simulator's own fleet names no node and is
  always served. Rows never name a node, though their timing can tell public
  reporters apart; nothing a withheld node reported is served. The map's replay
  places each fix at the time it was received and its panel says how old it
  is. `ADSB_SNAPSHOT_ENABLED=0` stops the snapshots.
- **archive read limit** — the map's archive reads, `/api/data/tracks`,
  `/api/data/tracks/availability`, `/api/data/detections` and
  `/api/data/adsb`, spend one budget per client address, kept in the app
  (`services/archive_rate_limit.py`): `ARCHIVE_RATE_BURST` reads per 10 s (30
  unless set) and `ARCHIVE_RATE_PER_MINUTE` per minute (120), both of which must
  have room. The address is the peer after uvicorn's trusted-proxy handling,
  so a forwarding header from anyone but the local nginx changes nothing, and an
  IPv6 /64 is one address. Past either window the answer is `429` with
  `Retry-After`, the seconds until every full window has room again. The
  addresses it counts are bounded in number, and when full it refuses addresses
  it does not hold rather than forget a live one. The Data Explorer's
  `/api/data/archive` listing and downloads are not counted, since its scans and
  download scripts read in bursts; nginx's per-IP `api` zone bounds
  them, as it does every route.
- **admin** (`admin.retina.fm`) — the dashboard bundle again, built at a root and
  serving the admin route table, user management among it, which
  `dashboard/src/utils/surface.ts` selects from the hostname. It keeps a name of
  its own because a Cloudflare Access application can only be scoped to one.
- **Illuminator search** — not a surface of this repo. tower-finder-service owns
  both the API and the UI, and serves `towers.retina.fm` from its own edge. The
  vhosts here proxy `/api/towers`, `/api/elevation` and `/api/config` to it.

## Data flow

```
legacy TCP nodes ──▶ tcp_handler ──┐
v1 nodes (POST) ───▶ node_stream ──┼─────────▶ frame_queue
stock blah2 ◀─GET── blah2_poller ──┘
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
- **`routes/tracks_archive.py`** — archive routes; `services/track_writer.py`
  stores solves with track key, lane, position sigma, anomaly flags, node kind,
  solve count and the refs their contributors were published under, one row
  group per kind. New rows take the node kind from the handshake verdict
  (`node_world` in `core/state.py`); older rows fall back to the contributor
  id prefix (`services/node_kind.py`). `tracks_store.py` serves them off the
  event loop, caching up to `TRACKS_CACHE_MAX_BYTES` (256 MiB default).
  Compaction and the cold cache have three more knobs,
  `TRACKS_COMPACT_RETENTION_DAYS`, `TRACKS_COLD_CACHE_MAX_BYTES` and
  `TRACKS_COLD_MISS_TTL_S`, described under the tracks archive above.
- **`routes/adsb_archive.py`** — the ADS-B archive route; `services/adsb_writer.py`
  names and writes the raw parts, `services/adsb_store.py` serves them (own
  cache, same bound as tracks, on the tracks archive's thread pool) and
  `services/tasks/adsb_snapshot.py` takes the snapshots from each reporter's
  latest fixes in `state.adsb_fixes_by_reporter`. `services/compaction.py`
  compacts it as its `adsb` store, with `ADSB_COMPACT_RETENTION_DAYS`,
  `ADSB_COLD_CACHE_MAX_BYTES` and `ADSB_COLD_MISS_TTL_S`, described under the
  ADS-B archive above.
- **`services/tasks/`** — background async tasks: `aircraft_flush` (broadcast),
  `adsb_snapshot`, `feed_gc` (stale-store GC on its own 5 s timer, deliberately
  not tied to the feed build), `solver` workers, `analytics_refresh`, archive lifecycle,
  snapshots, `health_monitor` + `heartbeat` (see [`alerting.md`](alerting.md)).
- **`services/mail_outbox.py`** — outbound mail. Sign-in and node-claim links
  are queued in the `mail_outbox` table and sent by a worker inside the app,
  with backoff and retry; `services/mail_events.py` pulls hard bounces and
  complaints from an optional Cloudflare Queue (see the runbook, "Outbound
  mail").
- **`core/state.py`** — the in-memory world: connected nodes, tracks, aircraft,
  arc buffers, WebSocket client sets, latest JSON payloads.
- **`core/users.py` + `core/auth.py`** — sign-in sessions (server-side, in
  SQLite) and domain auth: node ownership and emailed sign-in links.

## The algorithm libraries (submodules)

The math lives in separate repos under `libs/` so it can be versioned and reused:

- **retina-geolocator** — bistatic delay/Doppler position solver. Single-node
  produces an ellipse arc (a locus, not a point); multi-node (n≥2) runs an LM
  least-squares solve for a position, with an altitude sweep for n≥3.
- **retina-tracker** — Kalman multi-target tracker + anomaly detection.
- **retina-simulation** — synthetic fleet generator (powers the admin
  console's `/sim` map + CI), on the deployments that set
  `SYNTHETIC_FLEET_ENABLED`. Runtime-tunable via `PUT /api/simulation/config`,
  admin only (target-class fractions, aircraft counts), which the fleet polls
  every 5 s; fleet scale itself comes from the deployment env (`FLEET_*` in the
  compose files).
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
| `KNOWN_LANE_MODE` | `off/shadow/binding` | `binding` | `shadow` | identity-first known-target claiming: per-frame detections bound to live ADS-B hexes (`state.known_claims`) leave the dark pool before the tracker/association ever see them |
| `ADSB_FALLBACK_ENABLED` | unset/`1` | unset | `1` | adsb-service polled for claim candidates where real nodes send no ADS-B of their own (`services/tasks/adsb_fallback.py`) |
| `NODE_TRACKS_MODE` | `off/live` | `off` | `live` | a node that sends its own tracks (node API 1.6.0) is tracked on them in place of the server's tracker; set in `docker-compose.yml`, so every environment runs `live` |
| `TRACK_SMOOTHER` | `kf/ewma/off` | `kf` | `kf` | display smoothing for multinode tracks (`ewma` is the rollback) |
| `REPUTATION_PENALTY_SCALE` | float ≥ 0 | `0` | `0` | multiplier on every node-reputation penalty; `0` means no node can be blocked, `1` is the historical behaviour (temporary — see [`runbook.md`](runbook.md)) |

`shadow` computes and counts a stage's verdicts (exposed in
`/api/test/solver-stats`) without letting them bind — the standard soak step
before flipping `active`. Production sets none of the `off/shadow/active`
flags (all `off`). `KNOWN_LANE_MODE` differs from its siblings on both axes by
design: its acting value is named `binding` (a claim *binds* a detection to a
transponder identity), and its code default is that acting value, because
three consumers depend on the registry it fills (the known-lane solver, the
per-node trust residuals, and the feed's claimed-ADS-B display section,
`adsb_single_node`/`adsb_multi_node`).
Production and staging nevertheless run it in `shadow`, set with
`ADSB_FALLBACK_ENABLED=1` in their compose overlays, while the claims
adsb-service's traffic makes possible are measured; the test droplet keeps the
default.

## State & storage

- **In-memory first.** Tracks, aircraft, node sessions live in `core.state`.
  A restart drops them.
- **Snapshots.** State is serialized to disk every 60s and restored on boot
  (trust scores, reputations, accuracy samples, node identities).
- **SQLite** (`data/users.db`) — users, and node claims: who owns each node and
  the address it was claimed with. Also the mail outbox and the claim rate-limit
  counters. The outbox's bodies are encrypted under `data/mail-outbox.key`,
  which the daily R2 backup does not copy.
- **R2 (Cloudflare).** Archived coverage/track Parquet is offloaded to the
  `retina-server-archive` bucket and pruned locally (see the runbook).

## Auth model

A sign-in link mailed to the address starts a server-side session, shared
across surfaces on the same origin. The `auth_token` cookie holds a random
token; `users.db` keeps only its sha256 hash in `user_sessions`, with the user,
the created, last-used and expiry times, a short user-agent label and a coarse
network (IPv4 /24, IPv6 /48). A session expires after 7 days without use. Use
extends it, at most once an hour, and no session lives past 30 days; the cookie
lasts to that 30-day end and the server enforces the idle limit. Signing out
deletes the row, so a copied cookie stops working. A signed-in user can list
their sessions and revoke one or all the others on the account page, and can
delete the account there. An administrator can deactivate or reactivate an
account, mail it a sign-in link, start a change of its address or delete it.
Deactivating ends all of its sessions, and reactivating brings none back;
deleting ends them with the account. An open owner feed (`/ws/aircraft/owner`)
is closed with 1008 as soon as its session ends or its account is deactivated
or deleted, and re-checks its session every 30 s as a backstop.

Changing the address mails a link to the new one, and the address changes when
it is opened. The old address is retired to the account rather than freed: no
other account can take it, and sign-in or claim mail for it goes to the current
address.

Cookies signed with `JWT_SECRET` before sessions existed are still read: a valid
one is exchanged for a session on the console's next load. They lasted 7 days,
so after a week none is valid and the reader and the secret go. Nothing is
signed with the secret any more.

Administrators come only from Cloudflare Access, never from a database flag:
the origin verifies the `Cf-Access-Jwt-Assertion` itself against the team's published keys,
with `aud` pinned per environment to `CF_ACCESS_AUD`, and the verified email is
the identity (`backend/core/access_identity.py`). Enforcement is backend-side
because every vhost proxies `/api/` to the same app, so gating one hostname at
the edge would protect that hostname's HTML and nothing else; it is also why
`api.retina.fm`, the fleet's ingest hostname, carries no Access application.
`AUTH_ALLOW_ANONYMOUS_ADMIN=1` still grants the anonymous-admin bypass,
independent of `RETINA_ENV`, but only `docker-compose.local.yml` sets it. Node
ownership maps
`node_id → user_id`; the `/ws/aircraft/owner` feed and dashboard use it to scope
data to a user's own nodes. An owner's records of a node (report, configuration
history, events, SNR trend, archive) start when it became theirs
(`node_claims.owned_since`): what it recorded for a previous owner is not shown
to the next.
The owner feed authenticates by the session cookie, and refuses a handshake from
a page that is not the console's before reading it (`backend/core/origins.py`).

## Deploy

`.github/workflows/ci.yml`: push to `main` → build/test → deploy staging →
staging smoke + E2E → deploy production → prod smoke + E2E. Each environment's
deploy, verification and rollbacks live in a called workflow
(`staging-deploy-verify.yml`, `production-deploy-verify.yml`) called as a single
job, so one run holds that environment until its own verification, and any
rollback it triggers, has finished. Deploy is an
SSH `git reset --hard origin/main` + `docker compose up -d --build`, gated by a
free-disk pre-flight. Operational detail is in [`runbook.md`](runbook.md).
