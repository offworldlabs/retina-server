# Detection Pipeline

The core processing chain that takes raw detection frames from nodes and produces
positioned aircraft in the output feed.

---

## Overview

```
TCP frame (node)
    │
    ├─ ADS-B fast-path → state.adsb_aircraft (immediate, no queuing)
    │
    └─ frame queue (asyncio, capacity 10 000, sharded: crc32(node_id) % FRAME_WORKERS)
           │
           └─ FRAME_WORKERS workers, one per shard → thread pool
                  │
                  ├─ PassiveRadarPipeline.process_frame()
                  │       ├─ Tracker.process_frame()  (Kalman + GNN)
                  │       └─ _run_geolocation()        (LM solver)
                  │
                  ├─ node_associator.submit_frame()    (cross-node correlation)
                  │
                  └─ state.node_analytics.record_detection_frame()

Aircraft flush task (1 Hz)
    └─ build_combined_aircraft_json()
           ├─ single-node geolocated tracks
           ├─ multi-node solved tracks
           ├─ ADS-B aircraft
           └─ detection arcs (promoted tracks, no ADS-B)
                  │
                  └─ broadcast to WebSocket clients + write aircraft.json
```

---

## 1. TCP Frame Ingestion

Each node maintains a persistent TCP connection to the server on port 3012.
Frames arrive as newline-delimited JSON and go through a handshake sequence:

```
HELLO  →  CONFIG (node sends its geometry/freq config)
       ←  CONFIG_ACK (server confirms, assigns node_id)

DETECTION  →  (streams indefinitely, one frame per interval)
HEARTBEAT  →  (every 60 s when no detections)
```

On receipt the server does two things in parallel:

1. **ADS-B fast-path**: if the frame contains an `adsb` array, every entry is
   written directly into `state.adsb_aircraft` before the frame touches any
   queue. This keeps the ADS-B map current even if the frame queue is saturated.

2. **Frame queue**: the frame is enqueued for CPU-bound processing by the
   `FRAME_WORKERS` thread pool. `FRAME_WORKERS=8` on the production server.

   The queue is sharded by node: a frame goes to shard
   `crc32(node_id) % FRAME_WORKERS`, and each worker drains exactly one shard,
   awaiting one frame's executor call before it takes the next. So a node's
   frames are processed one at a time and in arrival order, while frames from
   nodes on other shards run in parallel. This is not a tuning choice: the
   per-node `Tracker` has no locking, and while every worker drained one shared
   queue two frames from the same node could mutate the same track list from two
   threads — an `IndexError` out of `_associate` (frame dropped), or, silently,
   out-of-order Kalman predict/update pairs. `FRAME_WORKERS=1` is a single shard
   and therefore a plain FIFO. Depth (`frame_queue_depth`,
   `frame_queue_saturated`) counts every shard, and the capacity of 10 000 is
   the budget for the queue as a whole.

   The cost of the mapping is that two busy nodes can land on one shard while
   another shard idles, so raise `FRAME_WORKERS` rather than expecting perfect
   balance from a fleet smaller than the worker count.

---

## 2. Kalman Tracker (retina-tracker)

Each node has its own `PassiveRadarPipeline` instance, and inside it a private
`Tracker` instance running standard M-of-N Kalman + GNN association.

**State vector**: `[delay_µs, doppler_Hz]` — the two bistatic observables.

**GNN (Global Nearest Neighbour) association**:
- Predicts each track one step forward with its Kalman filter.
- Builds a cost matrix using Mahalanobis distance as the gating metric.
- Solves the assignment with `scipy.optimize.linear_sum_assignment` (Hungarian).
- SNR-weights costs so high-SNR detections are preferred.
- ADS-B-initialized tracks get a 20% cost bonus to keep them associated.

**Track states** (following blah2 architecture):

| State | Meaning |
|-------|---------|
| `TENTATIVE` | Newly created, not yet confirmed |
| `ASSOCIATED` | Has received at least one update |
| `ACTIVE` | Promoted via M-of-N; assigned a track ID |
| `COASTING` | Missed last frame; gate expands to recover |

**M-of-N promotion**: a track is promoted from `TENTATIVE` to `ACTIVE` once
`n_associated >= M_THRESHOLD` (default 4) within an N-frame window (default 6).
Only at this point does it receive a stable `track_id` and get emitted to the
event writer for geolocation.

**Tracklet stitching**: when a new detection falls within
`TRACKLET_MAX_DELAY_RESIDUAL` and `TRACKLET_MAX_DOPPLER_RESIDUAL` of a recently
deleted track, it's linked rather than spawning a new hypothesis.

---

## 3. Geolocation (retina-geolocator, LM solver)

After each tracker frame, `_run_geolocation()` asks the event writer which
tracks have new data, then runs the Levenberg–Marquardt solver on each.

**Inputs**: a window of the last 20 detections in `{timestamp, delay_µs, doppler_Hz, snr}` form.
At least 3 detections are required before the solver is called.

**Initial guess**: `select_initial_guess()` uses the bistatic geometry to
enumerate candidate positions along the ellipsoid and picks the one whose
predicted delay/doppler best fits the most recent measurements. On subsequent
frames the previous solution is used as the warm-start (temporal continuity).

**Solver output** (`solve_track()`):
- 6-element state vector: `[east_km, north_km, up_km, vel_east, vel_north, vel_up]`
  all in km / km·s⁻¹, ENU relative to the receiver.
- RMS residuals for delay and Doppler.
- `success: bool` — false if the LM solver diverged or hit iteration limits.

The ENU solution is converted to WGS-84 `(lat, lon, alt_m)` via
`Geometry.ecef2lla` for output.

**Target classification** (per-node):
- `aircraft` — default; also auto-assigned when speed > 60 m/s or alt > 600 m.
- `drone` — speed ≤ 60 m/s and alt ≤ 600 m when `target_profile = "auto"`.
- `drone` profile nodes constrain the initial altitude guess and solver bounds
  for better convergence on slow, low targets.

---

## 4. Multi-Node Solver

Tracks from different nodes seeing the same target can be combined for a
tighter position fix. Association is **track-level**: each node's confirmed
tracklets (not raw detections) are submitted to the `InterNodeAssociator`
(`retina_analytics.association`), which pairs them across nodes on predicted
delay/Doppler consistency inside precomputed overlap zones, with one-to-one
χ² assignment. Candidate pairs go to the `solver_queue` for the LM multinode
solve (run in a process pool; `retina-geolocator.solve_multinode`, which also
reports per-solve east/north position covariance).

**Top-down claiming** (`ASSOC_CLAIM_MODE`, active on staging): after the
pairing round, established multi-node global tracks predict their expected
delay/Doppler at each node and *claim* matching tracklets directly — matched
tracklets are excluded from fresh pairing and emitted as anchored solver
inputs. This cuts track fragmentation (one stable key per object instead of a
new key per pairing) and recovers solves the bottom-up round would miss.

**Publication gates**, in order, for a converged solve:

- **rms residual / χ² trimming** — contaminated contributing nodes are
  trimmed and the solve retried before rejection.
- **beam/FOV gate** — each contributing node's solution bearing/range must be
  consistent with where that node can see. Under `FOV_MODE=active` this is
  the node's *learned* empirical FOV (section 7); n=2 uses the FOV alone,
  n≥3 uses it widen-only alongside the theoretical range rule.
- **displacement gate + n=2 confirmation** — an n=2 pairing publishes only
  after a constant-velocity fit over its observation window justifies it
  (5 unknowns vs 4 residuals means the residual gates cannot discriminate at
  n=2 on their own).
- **consensus** (`SOLVER_CONSENSUS_MODE`, active on staging) — a
  pairwise-intersection hypothesis stage refines n≥3 solves.

**Identity**: `multinode_key_decision` keys the published track, preferring
the anchor key of the claiming global track (mn-dark keys for targets with no
ADS-B seed) so repeated solves of one object share one identity. Display
positions are smoothed per key (`TRACK_SMOOTHER=kf|ewma|off`, default `kf` —
a CV Kalman filter; `ewma` is the env-only rollback).

Multi-node solved aircraft appear in the output with `type = "multinode_solve"`,
`n_nodes` set, and `contributing_node_refs` listed. No ambiguity arc is emitted
for these since the position is precisely known.

---

## 5. ADS-B Integration

Nodes can piggyback ADS-B data on their detection frames (embedded in the
`adsb` field). The simulation fleet does this for all aircraft that have
`has_adsb = True`.

When a geolocated track has an `adsb_hex` and there's a fresh ADS-B fix in
`state.adsb_aircraft`:
- The displayed position is dead-reckoned from the ADS-B fix rather than
  taken from the LM solver. This is more accurate and smoother.
- `position_source` is set to `"adsb_associated"`.
- No ambiguity arc is emitted — the position is already known precisely.

ADS-B entries expire 60 s after the position was *captured*, which is the
timestamp on the frame that carried it rather than the moment the backend
stored it. The two differ under queue backlog, and it is the capture time that
the staleness gates need. Where a frame carries no usable timestamp, receipt
time stands in and `adsb_capture_ts_fallback` counts it. The bound is
asymmetric: a frame may be `ADSB_CAPTURE_MAX_SKEW_S` behind server time,
because backlog makes that honest, but only `ADSB_CAPTURE_MAX_LEAD_S` ahead,
because a stamp in the future is never stale to any gate. After expiry the
aircraft falls back to the solver position.

Records also carry `recv_ms`, the server clock at the moment the position was
stored. Rules that compare a fix against another server-stamped event need
that one: `record_adsb_calibration` bounds the fix against the node's last
detection, and comparing a node-clock stamp against a server-clock one would
make that an NTP test rather than a co-timing test. Writes are guarded so a
replayed or backlogged frame cannot walk `last_seen_ms` backwards.

External truth (`state.external_adsb_cache`, polled from adsb.lol) is stamped
the same way, from each feed's own position time. `AdsbLolClient` resolves
tar1090's `seen_pos` against its own fetch and publishes an absolute
`captured_at`, so a row it serves again from its last-good cache during an
outage keeps its real age. Entries stay servable for `EXTERNAL_ADSB_MAX_AGE_S`
and are therefore tens of seconds old by construction, so the tighter gates
refuse them, calibration's 10 s among them. Consumers scoring solves against
them apply `EXTERNAL_TRUTH_MAX_AGE_S` instead, which is tighter again.

---

## 6. Aircraft JSON Builder (`build_combined_aircraft_json`)

Runs every 1 s in the `_aircraft_flush_executor` thread. Priority order
for deduplicated hex codes:

1. **Single-node geolocated tracks** (per-node LM solver) — with or without ADS-B.
2. **Multi-node solved tracks** — takes precedence over single-node for the same hex.
3. **ADS-B-only aircraft** — aircraft seen in ADS-B but not yet tracked by radar.
4. **Ground truth** (simulation only) — injected from the fleet orchestrator,
   keyed separately and not displayed as aircraft markers.
5. **Pending detection arcs** — bistatic ellipse arcs for promoted (non-TENTATIVE)
   tracks that don't have a known ADS-B position.

The result is broadcast to all WebSocket clients and written to
`tar1090_data/aircraft.json`. The broadcast fans its sends out with
`asyncio.gather`, so it costs one 5 s send timeout in total no matter how many
clients are wedged (`ws_send_timeouts` in `/api/admin/metrics` counts the
clients dropped that way).

**Stale-store GC does not run here.** `services/feed_gc.py`
(`prune_stale_stores` for `adsb_aircraft`, `known_claims`,
`ground_truth_trails`, `track_histories`, `track_arc_motion`,
`track_last_emit`/`track_gate_hold`, plus `prune_multinode_tracks` for the
lane-aware `multinode_tracks` expiry) runs on its own 5 s timer,
`services/tasks/feed_gc.py::feed_gc_task`. It used to run inside this builder,
which meant a stalled broadcast — or an idle `state.aircraft_dirty`, which
skips the build entirely — stopped GC server-wide while frame workers and
solver threads kept writing those stores. The builder still calls
`prune_multinode_tracks` (idempotent) before reading its snapshot, and then
only reads.

---

## 7. Empirical Coverage & Learned FOV

Each node accumulates an empirical picture of where it can actually see
(`retina_analytics.empirical_coverage`, 72 × 5° bearing bins, persisted on the
`coverage_data` volume). Under `FOV_MODE` (`off | shadow | active`) that
learned field-of-view replaces the theoretical beam wedge as the association
grid and solver beam gate; the theoretical beam (declared aim, or the
broadside+90° fallback for nodes that never declared one) is only a *prior*
that binds until a bin has evidence. Broadening is fast (3 ADS-B-calibrated
positives open a bin; 10 extend range to P95 × 1.25); shrinking requires
negative evidence over time (≥3 recorded disappearances spanning ≥10 min,
newer than the bin's last positive) — absence of traffic never shrinks.

**Under `KNOWN_LANE_MODE != off` the CLAIM lane is the only calibration
source.** The emit-loop path below is silenced, for a different reason per
mode. In `binding` (the default since #240, 2026-08-25) claiming strips every
detection it binds from the frame *before* the tracker sees it, so
`track.last_detection_adsb_hex` — the thing that path gates on — is only ever
set by the tagged detections claiming did **not** take: adverse selection, the
worst binds, and for a synthetic node nothing at all. Measured on the test
deployment 2026-09-13: every synthetic node's newest calibration point was
dated 2026-08-25, i.e. the path had been dead for 19 days, and the trickle real
nodes still received (newest 3–6 days old) was 5–41 % out of the node's own
declared wedge. In `shadow` nothing is stripped, so both paths would see the
same detection and record it twice.

**What counts as a calibration positive from a claim is deliberately narrow.**
`services/known_claiming.py::_calibration_from_claim` runs on every claim the
lane makes and records a point only when all five hold. They are charged in
order, so exactly one counter moves per claim and the six sum to the claim
count (`known_claims.calibration_recorded` /
`known_claims.calibration_rejected.*` in `/api/test/solver-stats`):

1. **not a hold or follow claim** (`calibration_claims_rejected_hold`) —
   neither has a fresh transponder fix behind it: a hold is the node's own
   prediction of its own measurement, a follow is the lane's own published
   solve, and both would feed the polygon what the polygon is used to judge;
2. **fresh fix at the frame instant** — `CAL_MAX_ADSB_AGE_S` (10 s), the same
   rule the emit path uses (`rejected_stale_fix`). Claiming itself tolerates
   45 s, because dead reckoning is what its age-scaled gate is for;
3. **tight residual, unscaled by fix age** — `CAL_CLAIM_DELAY_US` (3 µs) and
   `CAL_CLAIM_DOPPLER_HZ` (8 Hz) against the claim's own prediction
   (`rejected_residual`). The claim gate is 10 µs / 25 Hz, which is where it
   has to be to bind an echo at all; simulator measurement noise is σ 0.1–0.2
   µs / 2–4 Hz, so 3 µs / 8 Hz is still > 5σ at the noisy end while shrinking
   the delay × Doppler area a wrong aircraft can land in by ~10×;
4. **uncontested and exclusive** (`rejected_contested`) — no established dark
   global's projection inside the claim gate (the existing `contested` flag),
   **and** no other known hex whose predicted (delay, Doppler) for this frame
   lies inside the full age-scaled claim gate of this detection. The second
   half is why the path-2 candidate list is now built for every frame carrying
   detections and without skipping already-claimed hexes — the
   `claimed_hexes` skip moved to the assignment's column build, so claiming is
   unchanged and a path-1 tag is still judged against the rest of the cache;
5. **mature link** (`rejected_immature`) — `CAL_CLAIM_MIN_CLAIMS` (3) claims
   of this hex by this node with no gap over `CAL_CLAIM_STREAK_GAP_S` (10 s,
   frame time). The counterpart of the emit path's `n_detections >= 3`, kept
   in its own store in `known_claiming.py` rather than in the hold store,
   which `KNOWN_HOLD_MAX_GAP_S <= 0` disables entirely. Every non-hold,
   non-follow claim advances the streak, including ones that fail rules 3–4:
   those are still evidence of the link, just not clean samples.

The position recorded is the claim's fix **dead-reckoned to the frame
instant** — the position the assignment gated on, not the reported one — which
is why `record_claim_calibration` does not apply
`CAL_FIX_DETECTION_SKEW_S`: dead reckoning makes that skew zero by
construction.

Measured offline (`backend/scripts/calibration_attribution_bench.py`, 20
synthetic nodes, 3 seeds × 5 simulated minutes, truth read off the simulator's
per-detection hex before the tags are stripped): the greedy
`associate_detections_to_adsb` tags that fed the old path are 8.9–11.0 %
wrong-hex and 8.1–10.0 % out-of-wedge; every claim recorded ungated is
0.9–3.5 % / 0–1.3 %; the five rules give **0.0–0.1 % wrong-hex and 0.0 %
out-of-wedge at 51–82 points per node-minute**. The one exception is a node
with no ADS-B tags *and* path H enabled, where the yield collapses to ~0.1
points per node-minute because path H outranks path 2 and rule 1 refuses hold
claims — see the note at the end of this section.

**Under `KNOWN_LANE_MODE=off` the emit-loop path is unchanged**, and its own
rules still stand. The polygon is used to judge solves and gate association,
so it must be built only from evidence independent of both, and only from
*detections*:

- the position recorded is the aircraft's **reported ADS-B fix** (≤ 10 s old,
  `services/calibration.py`) — never a solver output;
- the node's track must have associated a real detection within
  `CAL_DETECTION_FRESH_S` (5 s) and have ≥ 3 detections — a track coasting on
  ADS-B enrichment is not evidence (`track_gates.py`);
- that newest detection must itself carry the track's own ADS-B tag — a track
  that identity-swaps onto an untagged target keeps a stale hex and would
  otherwise record the departed aircraft's position;
- the fix must be taken within `CAL_FIX_DETECTION_SKEW_S` (2 s) of the
  detection it is attributed to (the exit-smear rule; staging 2026-08-10);
- published solves record **nothing** for their contributing nodes, in every
  mode: that attribution rides on the very association the polygon judges, and
  under an active FOV gate it once formed a ghost → positive → wider-gate
  feedback loop.

**Known gap: a tagless node stops calibrating after one frame per link.**
Path H (the hold) runs *before* path 2 and every claim creates a hold, so from
the second frame of a link onwards a node that sends no `frame["adsb"]` is
claiming through path H — which rule 1 refuses, and which does not advance the
maturity streak either. Such a link therefore never reaches
`CAL_CLAIM_MIN_CLAIMS`. Nodes that do send tags are unaffected (path 1
outranks path H), which is the whole synthetic fleet and any receiver with its
own ADS-B correlation. The bench quantifies it: blind, holds on, 0.11–0.13
points per node-minute with 97 % of claims charged to `rejected_hold`; the
same run with `KNOWN_HOLD_MAX_GAP_S=0` gives 51–71. The fix is to let a hold
claim that was *refreshed against a live transponder fix*
(`extra["fix_refreshed"]`, the consistency rule in `_claim_holds`) be judged
on that fix's own prediction rather than the hold's — it is a path-2 claim in
everything but name. Not done here.

**What is published as a REAL node's detection area is evidence only.** Under
`FOV_MODE=off` — the default, and what production and test run —
`empirical_coverage.polygon` in `/api/radar/analytics` is built by
`EmpiricalCoverageState.to_polygon(evidence_only=True)`: a bin is drawn only
when its own count reaches `FOV_OPEN_MIN_POINTS`, at its own clamped P85;
holes of at most `EVIDENCE_GAP_MAX_BINS` (2 bins, 10°) inside a lobe are
bridged; every other unobserved bearing collapses to the receiver apex. There
is no theoretical clip. It used to be clipped to the node's declared
`beam_azimuth_deg`/`beam_width_deg`, which are configuration — most nodes'
aim was never surveyed — so measured bins outside the declared wedge were
zeroed (radar3, 2026-09-06: evidence in all 72 bins reaching 17–65 km,
published as a 120° pie slice). The theoretical beam is never published as a
detection area, and the map draws nothing for a node with no polygon rather
than a sector nobody measured. Under `FOV_MODE=shadow|active` the published
polygon is the learned wedge instead, which is itself evidence-derived.

**A SYNTHETIC node publishes its declared wedge instead.** The simulator emits
a detection only for an aircraft inside the node's declared cone
(`retina_simulation/world.py::_aircraft_in_detection_cone`: bearing within
`beam_azimuth_deg ± beam_width_deg/2`, range within `max_bistatic_range_km` on
the differential when declared, else within `max_range_km` on the RX
distance), so for a simulated node the cone *is* the detection area by
definition — and the evidence is the unreliable half, because the calibration
points come from ADS-B hexes bound to tracks and roughly a third of those
binds are to the wrong aircraft. Measured on test 2026-09-13,
`synth-GVL-SCAT-0032` (42° beam) held 3,037 calibration points of which 47 %
lay outside its wedge (34 % ignoring the two edge bins), 55 out-of-wedge bins
had opened, and the published polygon covered 71 of 72 bearings: a 42° beam
drawn as a disc. So `services/node_registration.py::register_node_blocking`
passes `declared_geometry_is_truth=is_synthetic_node(node_id)` to
`NodeAnalyticsManager.register_node`, and those nodes publish
`EmpiricalCoverageState.declared_wedge_polygon()` — the prior azimuth and
width at `_reach_at` on each bearing, with no clamp, no bins and no
minimum-points gate, so it is served from the moment the node registers.
Real nodes never take this path: their declared aim is unsurveyed
configuration, which is exactly what the paragraph above exists to keep off
the map. Every summary names its rule in `empirical_coverage.polygon_source`
(`declared` / `evidence` / `learned`), and the map quotes a declared beam only
when it reads `declared` (`frontend/src/components/map/nodeSites.ts::coverageLine`).
The fuzz rewrite is unchanged: `public_location.translate_polygon` shifts a
declared wedge rigidly like any other polygon.

**Every public node payload carries a `node_ref`.** `/api/radar/analytics`
(both variants), `/api/radar/analytics/{node_id}` and `/api/radar/nodes` each
carry one per node: the registry's `Node.node_ref` for a node registered
through `/v1/nodes`, and otherwise an HMAC-derived ref of the same
`nde` + 12 base36 shape, keyed on the node fuzz salt under a `node_ref|`
domain (`backend/services/node_ref.py`). Nodes on the blah2 bridge or the
plain TCP protocol have no registry row, so without the derivation half the
fleet would have no public name at all. The map shows only `node_ref`; the
`node_id` remains the join key on the wire and in the client.

`CALIBRATION_SCHEMA` (currently 6) versions what a stored positive *means*;
persisted state with an older schema is discarded and relearned at node
registration, on every deployment, with no operator action (the ledger of
past bumps is in `empirical_coverage.py`).
