# Keyed data API

Date: 2026-10-28
Repo: retina-server
Status: design, awaiting review

Gives contributing users and paying customers programmatic access to the
network's aircraft data, with per-account keys, filters and history, in the
manner of FlightAware's AeroAPI. Tiers and billing are out of scope for v1, but
the entitlement model is shaped so they can be added without reworking it.

The decisions below were agreed in a Q&A session on 2026-10-28.

## Current state

- No customer-facing keys exist. `X-API-Key` is one shared `RADAR_API_KEY` from
  `backend/.env`, and it guards only write routes (detection ingest, custody,
  tower-selection stats). `WS_AUTH_TOKEN` is one shared `?token=` for the
  aircraft websockets.
- Aircraft data is unauthenticated: `GET /api/v1/solver/aircraft`
  (`routes/output.py`, one filter, `real_only`), the tar1090 snapshots under
  `/api/radar/data/`, `/ws/aircraft`, `/ws/aircraft/live` and
  `/api/radar/stream`. Nodes registered private are already redacted from these
  (`services/publication.py`).
- Accounts (fastapi-users, magic-link sign-in, JWT cookie; `core/users.py`)
  exist for node owners and administrators. They carry no entitlement, tier or
  quota.
- Solver tracks are written to Parquet under `coverage_data/tracks/` and
  uploaded to R2 by `archive_lifecycle`, but no route reads them. There is no
  lookup by aircraft and no history query.
- Rate limiting is per IP (nginx `api` zone, the in-process ingest limiter) and
  per node token on `/v1/nodes`. Nothing is per account.
- Node activity is recorded only as `nodes.last_seen_at`; there is no uptime
  history.

## Who gets access

An account is **entitled** when any of these holds:

- **Contributor.** It owns a claimed node that reported data on at least 50% of
  the last 30 days. Threshold and window are configuration, not constants.
  Entitlement is re-evaluated daily; when a contributor drops below the
  threshold, access continues for a grace period (configurable) before lapsing.
- **Customer.** An administrator granted it in the admin console, with an
  optional end date after which it lapses.
- **Administrator.** Always entitled.

### Data model

- `entitlements`: `user_id`, `kind` (`contributor` | `customer`), `tier_id`,
  `source` (`uptime` | `manual`, later `stripe`), `starts_at`, `ends_at`
  (nullable), `lapsed_at` (nullable). Contributor rows are maintained by the
  daily job; customer rows by the admin console, and later by a Stripe webhook
  writing the same fields.
- `tiers`: `id`, `name`, `requests_per_minute`, `max_concurrent_streams`,
  `max_history_span_s`. v1 seeds two: a contributor default and a customer
  default. Values are set before launch.
- `api_keys`: `id`, `user_id`, `name`, `prefix` (displayed), `secret_hash`,
  `created_at`, `last_used_at`, `revoked_at`.
- `node_activity_days`: `node_id`, `day`. One row per node per UTC day on which
  it delivered accepted detections. This is the uptime record the contributor
  check needs, and it does not exist yet.

## Keys

- Self-serve: an entitled user creates, names and revokes keys in the dashboard.
  The secret is shown once and stored hashed. Administrators can revoke any key.
- Sent as `x-apikey: <key>` (AeroAPI's header) or `Authorization: Bearer <key>`;
  websockets take `?apikey=`.
- A key authorises only while its owner is entitled. Revoking an entitlement
  disables every key on the account without deleting them.
- A signed-in entitled user's session cookie is accepted wherever a key is, so
  the dashboard needs no key of its own.

## What is gated

Keyed (key or entitled session), real nodes only — the synthetic fleet is never
served on these routes:

- `/api/v1/solver/aircraft`, `/api/v1/ground-truth/*`
- `/api/radar/status`, `/api/radar/nodes`, `/api/radar/analytics*`,
  `/api/radar/anomalies`, `/api/radar/accuracy`, `/api/radar/association/*`,
  `/api/stats/summary`
- `/ws/aircraft`, `/api/radar/stream`
- every new endpoint below

Public:

- The detection archive, `/api/data/archive*`, stays fully public. Note that its
  files carry node geometry and raw delay/Doppler, from which positions can be
  re-derived with a solver. That is accepted.
- The anonymous live map. Its feed becomes **viewport-bound**: the client sends
  its visible bounding box and the server returns only aircraft inside it, with a
  cap on the box's size so the whole network cannot be requested in one view.
  This makes bulk scraping slower, not impossible.

Write routes keep their existing `RADAR_API_KEY` and node-token auth; this work
does not touch them.

## Endpoints (v1)

Response bodies use the readsb/tar1090 `aircraft.json` shape (`hex`, `flight`,
`lat`, `lon`, `alt_baro`, `gs`, `track`, …) so existing ADS-B tooling can parse
them, with RETINA's quality fields alongside.

- **Live search.** Current positions filtered by bounding box or point and
  radius, altitude range, ground-speed range, `hex`, and callsign.
- **Lookup.** Current state and recent track for one `hex` or callsign.
- **Track history.** Positions from the stored track Parquet, by `hex` and/or
  area, between `start` and `end`. Everything stored is queryable, but one
  request may span at most the tier's `max_history_span_s`, and results are
  paged with a cursor.
- **Keyed streaming.** WebSocket and SSE, authenticated by key, applying the
  live-search filters on the server.

Rate limits are per key, by tier: requests per minute, and concurrent streams.

The keyed routes get a generated OpenAPI contract, as `/v1/nodes` has
(`contracts/nodes-v1.openapi.yaml`, `just contract`), since external clients
will pin to it.

## Rollout order

1. Entitlements, tiers, keys, the `node_activity_days` record and the daily
   contributor job; the dashboard key page; admin grant/revoke.
2. The new v1 endpoints behind keys.
3. Gate the existing read routes listed above.
4. Viewport-bound public map feed, with the map client changed in the same step.
   Confirm first which clients read `/ws/aircraft/live` and
   `/api/radar/data/aircraft*.json` (app.retina.fm may not be built from this
   repo), since a viewport-bound feed breaks any that are not updated.

## Later (TODO)

- **Metered billing, and large requests with it.** Once a metered billing model
  is implemented, large API requests — history spans beyond the tier cap, bulk
  exports — become available, billed by usage instead of refused. This needs
  per-account usage counting (requests, rows or bytes returned), likely an
  asynchronous export path that returns a download link for very large ranges,
  and Stripe-driven entitlements (`source = stripe`).
- Tier definitions beyond the two defaults.

## Open values

To be chosen before launch; all are configuration:

- per-tier requests per minute, concurrent streams and maximum history span
- the contributor grace period
- the public map's maximum viewport size
