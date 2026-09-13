# Node location privacy: private or fuzzed, nothing in between

Date: 2026-09-10. Branch `feat/node-location-privacy`.

## Decision

A node's location is published in exactly one of two ways.

| choice | what the public sees | what the network gets |
|---|---|---|
| **private** | Nothing that locates the receiver: no marker, no uncertainty disc, no coverage polygon, no ambiguity arcs, no single-node aircraft products, no `receiver.json` coordinate, no archive listing. | Everything. The node runs its own pipeline, joins track association and contributes to multinode solves; the solve is published with the private id struck from `contributing_node_ids` / `detecting_nodes`. |
| **public** | The receiver displaced by the deployment's deterministic donut fuzz (`services/public_location.py`), with `location_uncertainty_km` declared beside it. | Everything. |

There is no third state. A per-node fuzz opt-out (publishing a precise
receiver) was considered as part of the fuzz work and is now rejected: the
fuzz is the deployment's floor, not a preference. This note replaces the
"per-node fuzz opt-out" item in the phase-3 backlog.

## What already exists

`Node.publication` (`"public"` / `"private"`, migration 0002) records the
answer the owner gives at board onboarding (`routes/node_register.py`,
`PublicationChoice` in `routes/node_schemas.py`). Since #261 it is enforced on
every unauthenticated surface by `services/publication.py`; the redaction rules
in that module's docstrings are exactly the "private" column above and are not
changed here. The solver, `node_associator` and the per-node pipelines never
consult it, which is what keeps a private node in the solves.

## What this change adds

### 1. The choice can be made outside onboarding

Today only a board registration writes `Node.publication`, and a node that never
registered through the v1 API (a legacy TCP node, the synthetic fleet, a node
mirrored onto the test droplet) has no row at all. Owners and admins need to set
it from the dashboard, for any node id the system knows by string.

**Storage: one new table, `node_location_privacy`** (migration `0006`,
`rollback_safety = "additive"`), model in `core/nodes.py`:

| column | type | meaning |
|---|---|---|
| `node_id` | String(255) PK | same key space as `node_owners.node_id` (no FK to `nodes`: the table must accept ids that never registered) |
| `private` | Boolean, not null | the override |
| `set_by` | String(255) | user id, or `admin:<email>` |
| `set_at` | Float | unix seconds |

**Precedence: an override row wins over the registration choice; deleting the
row returns the node to its registration choice; no row and no registration
means public.** A re-registration (reflash) still rewrites `Node.publication`
as it does today and does not touch this table, so an owner's dashboard choice
survives a reflash. Effective private set:

```
private = (registration_private − override_public) ∪ override_private
```

computed inside `services/publication._query()` in one connection (two
`select`s), behind the same 30 s TTL cache. Add `publication.invalidate()`
(drops `_expires_at` under the lock) and call it from every route below after a
successful write, so a change is honoured on the next 1 Hz flush rather than up
to 30 s later. The cache's failure semantics (serve last known set, empty before
first success) are unchanged and must stay tested.

**Routes.** Owner (cookie auth, `get_current_user`), all under
`routes/auth.py` next to `/me/nodes`:

- `GET /api/auth/me/nodes` — each entry gains
  `location_private: bool` (effective) and
  `location_privacy_source: "default" | "registration" | "override"`.
- `PUT /api/auth/me/nodes/{node_id}/location-privacy` body `{"private": bool}`
  → `{"node_id", "location_private", "location_privacy_source": "override"}`.
  A node the caller does not own is **404** (`"Node not found"`), never 403:
  the two answers differ only in confirming the id exists, and the id space is
  guessable.
- `DELETE /api/auth/me/nodes/{node_id}/location-privacy` — removes the
  override → returns the effective state with its new source. 404 as above.

Admin (`require_admin`), under `routes/admin.py` next to `/nodes/{node_id}/owner`:

- `GET /api/admin/nodes/{node_id}/location-privacy` → effective state,
  source, and the raw pieces (`registration_choice: "public"|"private"|null`,
  `override: {private, set_by, set_at} | null`).
- `PUT` / `DELETE` — same bodies as the owner routes, `set_by = "admin:<email>"`,
  `log_event("user", ...)` like the owner-assignment route does.

None of these are `/v1/nodes` routes, so the node contract does not move
because of them.

### 2. The owner still sees their own private node

The owner feed (`/ws/aircraft/owner`) already serves an owner their private
node's aircraft and arcs, but the map's node markers come from
`/api/radar/analytics`, which drops private nodes for everyone — so an owner
who goes private loses their own dot, disc and coverage, and the dashboard's
node page 404s on `/api/radar/analytics/{node_id}`.

Fix in `routes/analytics.py`, without changing the unauthenticated behaviour or
its cached bytes:

- `GET /api/radar/analytics[?real_only]`: read the `auth_token` cookie
  *optionally* (a helper that returns `None` rather than raising; never a 401
  on this route). If the user is active and owns at least one node that is in
  the private set, parse the cached public bytes, add
  `public_node_summary(nid, state.node_analytics.get_node_summary(nid))` for
  each such owned node (same fuzzed frame the public gets — the owner is not
  admin; only admin surfaces show truth), and return that. Everyone else gets
  the cached bytes untouched (identity, no re-serialisation).
- `GET /api/radar/analytics/{node_id}`: the `is_private` 404 is bypassed when
  the optional user owns `node_id`.

Nothing else in the owner path changes. In particular `public_aircraft_payload`
and `filter_payload_to_nodes` are untouched.

### 3. The contract says what the choice means

`PublicationChoice` in `routes/node_schemas.py` currently reads "whether the
owner chose to publish this node's detections". Rewrite the class docstring and
the `choice` field comment to the semantics in the table above (private =
location withheld, still contributes to multinode solves; public = published
with the receiver displaced by the deployment's location fuzz), regenerate
`contracts/nodes-v1.openapi.yaml`, and bump `NODE_API_VERSION` 1.1.3 → 1.1.4
(a wire-visible description changed; nothing structural did). No change to
`Agreements`, `_apply_agreements`, or any request/response shape.

### 4. Dashboard

- `pages/user/NodeDetailPage.tsx`: a **Location privacy** card, shown only
  when the node is in `api.myNodes()`. Two-option control (radio or segmented,
  matching the page's existing controls): *Public* — "Shown on the map at an
  approximate position, displaced up to N km" (N from
  `detection_area.rx.location_uncertainty_km` when present, else "by the
  network's location fuzz"); *Private* — "Hidden from the public map: no
  marker, no coverage, no detection arcs, no single-node aircraft. Still
  contributes to multi-node solves." A line under it names the source
  ("Set at onboarding" / "Set here on <date>"), and when the source is
  `override`, a small "Use the onboarding choice" link that calls DELETE.
  Optimistic update with rollback on error; disabled while saving.
- Wherever the dashboard lists the user's own nodes (Overview / nodes list),
  a compact **Private** badge on private ones.
- `pages/admin/NodeManagementPage.tsx`: the same two-option control per node
  (admin routes), plus the badge.
- `api/client.ts`: `myNodeLocationPrivacy(nodeId, private)`,
  `clearMyNodeLocationPrivacy(nodeId)`, `adminNodeLocationPrivacy(nodeId)`,
  `setAdminNodeLocationPrivacy(nodeId, private)`,
  `clearAdminNodeLocationPrivacy(nodeId)`.
- Vitest coverage for the control (renders both states, PUT on change,
  DELETE on reset, rollback on failure).

The live map (`frontend/`) needs no change: redaction is server-side and the
owner's dot returns through §2.

## Not in scope, recorded

- Parquet detection files for a private node are still written (with the
  fuzzed receiver, as for any node) and uploaded to R2; the public API and the
  Data Explorer list through `/api/data/archive`, which hides them. The bucket
  itself is not a public listing surface today. If it ever becomes one, the
  writer must skip private nodes.
- Track archives (`track_writer.py`, `contributing_node_ids`) upload
  unfiltered — ids only, no geometry. Known since #261.
- The onboarding wizard in retina-gui owns the wording of the question it asks;
  the contract text here is what it should align to.

## Verification (test droplet)

The test droplet's `nodes` table is empty (mirrored and synthetic nodes never
register), so the override table is the only way to make a node private there.
Set one synthetic node private through the admin route, then on
`test-map.retina.fm`: its marker, disc, coverage and arcs are gone; multinode
solves it contributed to are still drawn and `contributing_node_ids` no longer
names it; `/api/radar/analytics/<id>` is 404; `/api/radar/nodes` omits it.
Clear the override and everything returns within one flush.
