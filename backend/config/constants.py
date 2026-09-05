"""Centralised constants shared across the RETINA backend.

Import from here instead of scattering magic numbers through services.
Values that are tunable per-deployment stay as env vars (FRAME_WORKERS,
SOLVER_WORKERS, etc.) — this file holds compile-time constants and the small
pure helpers that operate on them.

retina_tracker YAML config stays separate (loaded at runtime via config.yaml).
"""

import math
import os

from core.env_parsing import parse_comma_list

# ── Physics ──────────────────────────────────────────────────────────────────
C_KM_US = 0.299792458  # Speed of light (km/µs)
R_EARTH_KM = 6371.0  # Mean Earth radius (km)
FT_TO_M = 0.3048  # Feet → metres
KNOTS_TO_MS = 0.514444  # Knots → metres per second

# ── Field coercion ───────────────────────────────────────────────────────────


def is_num(v) -> bool:
    """True when v is a finite number and arithmetic on it is meaningful.

    The complement of as_num()'s fallback: a field this rejects carries no
    measurement, so a truth comparison must drop the sample rather than score
    against the substituted 0.0.
    """
    return isinstance(v, (int, float)) and math.isfinite(v)


def as_num(v) -> float:
    """0.0 for anything that isn't a finite number (ADS-B's "ground", None, NaN).

    tar1090 reports alt_baro as the literal string "ground" for aircraft on the
    ground.  Coerce before any arithmetic on a raw ADS-B field.  Sound for a
    solver seed; use is_num() where the value is compared against truth.
    """
    return float(v) if is_num(v) else 0.0


# ── Association gates ────────────────────────────────────────────────────────
DELAY_MATCH_THRESHOLD_US = 15.0  # Bistatic delay tolerance for matching
ASSOC_GRID_STEP_KM = 3.0  # Overlap zone grid resolution (km)
ASSOC_MIN_INTERVAL_S = 30.0  # Per-node association rate limit (s)
ASSOC_MAX_NEIGHBORS = 50  # CPU budget cap for neighbor checks
# Track pairings emitted per association round when the constant-velocity fit
# is deferred to the solver worker, which is the production configuration.
# Distinct from the associator's inline-fit budget (8): that one bounds ~86 ms
# LM solves, this one bounds queue pressure — a _merge_epochs, a candidate
# carrying those epochs, and a solver-queue slot each.  They were the same
# number by accident, and since the fit budget only decrements when a fit runs,
# on the deferred path it silently capped pairings at 8 per node pair.
ASSOC_MAX_PAIRS_PER_ROUND = 64

# ── n=2 confirmation (track-to-track association) ────────────────────────────
# The frame path pairs confirmed single-node tracks; the solver worker runs the
# constant-velocity fit.
#
# Measured blind, track association takes the n=2-only ghost rate from 92.9% to
# 55.4% at no cost in real tracks.  The fit that makes that work is an ~86 ms LM
# solve, and running it inside the frame worker took staging to 92% frame-queue
# depth with the processor 21 s behind a 6 frame/s feed.  It now runs on the
# solver's own threads, behind its own queue and staleness drop, so the frame
# path keeps only the coarse delay gate — one BLAS contraction per node pair.
#
# This now controls only solver.py's _N2_REQUIRE_CONFIRMED — whether an n=2
# solve must carry a chi2 confirmation before its track is published.  The
# detection-level fallback it used to select has moved out of the production
# import graph entirely (retina_analytics.detection_association, reachable from
# the offline bench), so there is no second path left to switch to.
N2_TRACK_ASSOCIATION = True

# At n=2 a single-epoch solve has 5 unknowns against 4 residuals, so rms_delay
# and rms_doppler go to zero for a cross pairing exactly as for a real target
# and neither can gate it.  A pairing of two *confirmed single-node tracks*
# supplies 4K measurements against 6 unknowns instead, and the constant-velocity
# fit over them produces a real chi2.  See retina_analytics.association.
N2_CONFIRM_CHI2_MAX = 2.0  # chi2/dof ceiling for an n=2 track pairing
N2_CONFIRM_MIN_SPAN_S = 12.0  # Observation span before a pairing is fitted
N2_CONFIRM_MIN_EPOCHS = 4  # Floor on samples; span is the real gate
N2_TRACK_HISTORY_MAX = 20  # Per-node track samples fed to the fit

# A 2-node track needs this many solves before it renders a plane; 1
# disables the gate.  One-shot n=2 solves were the dominant ghost source.
MN_N2_MIN_SOLVES = int(os.getenv("MN_N2_MIN_SOLVES", "2"))
# One-shot display lifetime for n>=3 solves, seconds.  A track confirmed by
# a second solve gets the normal 60 s entry expiry / 30 s DR cap.
MN_ONESHOT_TTL_S = float(os.getenv("MN_ONESHOT_TTL_S", "5.0"))

# Quality gate for adopting the constant-velocity fit's velocity into a
# published solve, in place of the single-epoch Doppler solution (see
# solver.py's _resolve_cv_fit / velocity adoption in _process_solver_item).
# Velocity is Doppler-determined — one projection per node — so n=2 is
# underdetermined and n=3 exactly determined, and single-epoch noise maps
# straight into the vector; the CV fit ties the whole observation window to
# one constant-velocity trajectory instead.  Distinct from N2_CONFIRM_CHI2_MAX,
# which gates whether an n=2 *track* publishes at all — that stays unaffected;
# this only decides which velocity a solve that is already publishing shows.
CV_VEL_ADOPT_CHI2_MAX = float(os.getenv("CV_VEL_ADOPT_CHI2_MAX", "5.0"))

# Oldest ADS-B fix still usable as a coverage calibration point.  At 250 m/s a
# 10 s fix is 2.5 km stale, which is already coarse against a 5°/72-bin polar
# grid; beyond that the point stops describing where the target was when the
# node detected it.  The frame path used to apply no age gate at all and took
# whatever _fresh_adsb returned — up to 60 s, i.e. 15 km.  See
# services/calibration.py, which both recording sites now go through.
CAL_MAX_ADSB_AGE_S = 10.0

# Oldest last-detection stamp still allowed to characterize node coverage.
# Detection events arrive at the ~1 Hz frame cadence, so 5 s tolerates a
# couple of dropped frames while bounding a 250 m/s target to ~1.25 km past
# its last actual detection — the same staleness budget CAL_MAX_ADSB_AGE_S
# applies to the fix itself. The emit path stays alive DISPLAY_STALE_TRACK_S
# (15 s) past the last detection for display; calibration must not inherit
# that window, or the coverage polygon paints an aircraft's exit path as
# detected ground.  See services/track_gates.py.
CAL_DETECTION_FRESH_S = 5.0

# Tightest bound between a calibration point's ADS-B fix and the detection
# event it is supposed to describe.  A calibration point must describe where
# the target WAS WHEN THE NODE DETECTED IT — but CAL_DETECTION_FRESH_S and
# CAL_MAX_ADSB_AGE_S above are both gates measured against `now`, the moment
# the emit loop happens to run, not against each other.  So for up to
# CAL_DETECTION_FRESH_S after the last real detection, the emit loop kept
# recording the LIVE fix while the aircraft flew on: past the wedge edge at
# medium range, or — near the RX, where 1 km of travel is on the order of 60°
# of bearing — at an arbitrary bearing entirely.  Bounding
# |fix_ts - detection_ts| pins the recorded position to the detection event
# itself, independent of how fresh either one is relative to now: 2 s at
# 250 m/s is <= 0.5 km of skew, well inside the 5°-bin quantisation at the
# ranges where this matters.  See services/calibration.py, the one place
# that enforces it, and services/track_gates.py, which supplies both
# timestamps.
CAL_FIX_DETECTION_SKEW_S = 2.0

# ── ADS-B seeding (ADSB_SEED_MODE) ────────────────────────────────────────────
# A track view exports its ADS-B tag only if one of the newest N history
# detections carries it.  A swapped track's newest detections go untagged
# while track.adsb_hex stays stale — see the identity-swap note in
# services/track_gates.py — so one untagged newest frame is a receiver
# hiccup (still export the tag), but N consecutive untagged is the swap
# signature (withhold it).
ADSB_VIEW_TAG_FRESH_N = 3

# ── Known-target claiming (KNOWN_LANE_MODE) ──────────────────────────────────
# How long a hex stays in state.known_claims after its last claim.  2x the
# 60 s the ADS-B cache itself ages at (feed_gc): once the cache entry is gone
# no new claim can form for the hex, but the trust path reads a hex's residual
# *trend* out of the registry, and pruning on the cache's own horizon would
# wipe that history mid-assessment on a brief feed dropout.
KNOWN_CLAIMS_STALE_S = 120.0

# How recent a claim must be to put its hex on the map as an `adsb_single_node`
# entry.  Far tighter than the registry's own retention above, which exists to
# keep a residual *trend* readable: this one answers "is that node detecting
# this aircraft right now", and a claim older than a few frames is not evidence
# of a live detection.  5 s matches CAL_DETECTION_FRESH_S (the same "a couple
# of dropped frames" budget at the ~1 Hz frame cadence) and the horizon the
# frontend fades a claimed arc over, so the arc and the icon vanish together
# instead of one outliving the other.
CLAIMED_DISPLAY_FRESH_S = 5.0

# ── Default antenna parameters ───────────────────────────────────────────────
YAGI_BEAM_WIDTH_DEG = 42.0  # Half-power beamwidth (°) of the fleet Yagis
YAGI_MAX_RANGE_KM = 50.0  # Default Yagi max range (km)

# ── Single-node ambiguity arcs ───────────────────────────────────────────────
# Floor on the measured differential range (delay_us * C_KM_US) below which no
# ambiguity arc is emitted.  A near-zero differential means the delay ellipse
# collapses onto the TX–RX baseline, so the "arc" renders as a tiny sliver
# hugging the baseline — a misleading blob that conveys no positional
# information.  Staging arc-UX diagnosis (2026-08): 36 of 415 emitted arcs
# were such <5 km blob stubs, median differential 2.6 km, many traced to
# clutter returns.  3.0 km clears that measured stub population.  The track
# itself still emits below the floor (position only, no arc) — see the
# floor exemption in services/track_gates.py's track_entry.
ARC_MIN_DIFFERENTIAL_KM = 3.0

# Anomaly types an arc-only (no fresh ADS-B) track may carry.  A single-node
# delay measurement constrains almost nothing about behaviour, so most anomaly
# streams are meaningless noise there — only physically loud signals survive:
# supersonic Doppler and extreme acceleration.  Everything else (orbits,
# identity swaps, position mismatches) needs either ADS-B truth or a
# multi-node solve to mean anything.
ARC_ONLY_ANOMALY_ALLOWLIST = frozenset(
    {
        "supersonic",
        "instant_acceleration",
        "anomalous_acceleration",
    }
)

# ── Track & history limits ───────────────────────────────────────────────────
TRACK_HISTORY_MAX = 60  # Rolling position buffer per aircraft
GROUND_TRUTH_MAX = 120  # Ground truth trail length
ANOMALY_LOG_MAX = 500  # Max anomaly log entries

# ── Flush & refresh intervals (seconds) ──────────────────────────────────────
AIRCRAFT_FLUSH_INTERVAL_S = 1.0  # aircraft.json write + WS broadcast cadence (1 Hz)
ANALYTICS_REFRESH_INTERVAL_S = 30  # Background analytics recompute
# Detection archive batch write — one Parquet file per node per hour.
# At ~1 fps that's ~3600 frames/hour ≈ 300 KB zstd Parquet, which is the
# minimum size we want for analytics queries; smaller files (the previous
# 30 s cadence) create the small-files problem at scale.
ARCHIVE_FLUSH_INTERVAL_S = 3600
ARCHIVE_BATCH_MAX = 10000  # Safety cap; should not normally trigger
TRACK_ARCHIVE_FLUSH_INTERVAL_S = 60  # Multi-node solver track archive flush cadence

# ── Detection mirror (production forwards accepted v1 frames elsewhere) ──────
DETECTION_MIRROR_FLUSH_INTERVAL_S = 1.0  # One batched POST per second
# Ten flush intervals at the contract's ceiling (12 nodes at 2 Hz = 24 frames/s).
# Deliberately small: the mirror sheds rather than accumulating a backlog when
# the receiving environment is unreachable.
DETECTION_MIRROR_QUEUE_MAX = 240
DETECTION_MIRROR_TIMEOUT_S = 2.0  # Bounds a hung receiver
DETECTION_MIRROR_LOG_INTERVAL_S = 60.0  # Throttle for the drop/failure line

# ── Archive lifecycle (R2 offload + local disk cleanup) ──────────────────────
ARCHIVE_OFFLOAD_AGE_DAYS = 1  # Upload to R2 after this many days
# Must be > ARCHIVE_OFFLOAD_AGE_DAYS. Files land in R2 after 1 day, then local
# copies are deleted after 3 days (2-day retry window if R2 upload fails).
ARCHIVE_RETENTION_DAYS = 3  # Delete local copy 3 days after creation
ARCHIVE_LIFECYCLE_INTERVAL_S = 3600  # Run lifecycle check every hour

# ── users.db backup to R2 ────────────────────────────────────────────────────
# Daily snapshot via VACUUM INTO + upload to R2 under backups/users-db/.
# 30 days is the cheapest "sane" retention: enough to recover from a
# realised-too-late deletion (typical "I revoked the wrong invite" scenario
# manifests within a week), short enough that the bucket doesn't grow
# unbounded as the user table itself does.
USERS_DB_BACKUP_INTERVAL_S = 86400  # Once per day
USERS_DB_BACKUP_RETENTION_DAYS = 30  # Keep last N daily snapshots in R2

# ── Geolocation solver ───────────────────────────────────────────────────────
GEO_INTERVAL_S = 10.0  # Per-track solver rate limit (seconds)
PRUNE_INTERVAL_S = 60.0  # Stale-entry pruning interval (seconds)
STALE_TRACK_S = 120.0  # Remove tracks not updated in this window

# How long a track with no fresh detections keeps appearing in aircraft.json.
# Distinct from STALE_TRACK_S, which controls when the tracker *forgets* the
# track entirely.  Single-node detections are intermittent, so dropping a track
# the instant detections pause makes aircraft flicker; but STALE_TRACK_S=120 s
# meant a stationary icon was painted for two minutes while the real aircraft
# flew ~17 km away.  Within this window the position is dead-reckoned from the
# track's own velocity so it keeps pace instead of freezing; past it the entry
# stops rendering while the tracker keeps its state for re-acquisition.
DISPLAY_STALE_TRACK_S = 15.0

# Maximum time the arc speed/RMS gates may hold a track at its last emitted
# position.  The gates exist to absorb a single mis-associated frame; without a
# bound they latch (the reverted position becomes the next frame's reference)
# and freeze the icon indefinitely.  ~3-5 frames: long enough for the intended
# job, short enough to cap the resulting error at speed x 10 s (~1.5 km).
GATE_MAX_HOLD_S = 10.0

# How long a ground-truth trail keeps being served after its last push.
# The simulation orchestrator pushes ground truth every 2 s, so a live
# aircraft's trail tip is never more than ~4 s old.  This was previously
# folded into a shared 300 s constant, which meant an aircraft that despawned
# (lifetime expiry in world.step) stayed on the map as a stationary blue dot
# for five minutes — measured at 3 of 16 dots being such ghosts, with trail
# tips 243-284 s old and exactly 0.00 km of movement.  10 s is five push
# intervals: tolerant of a dropped push or two, without the ghosting.
# Same reasoning as DISPLAY_STALE_TRACK_S, which fixes this for radar tracks.
GT_DISPLAY_STALE_S = 10.0

# How long render trails (track_histories) survive without updates.  Longer on
# purpose: these draw the path *behind* an aircraft, so a generous tail is the
# desired behaviour rather than a staleness bug.
TRAIL_STALE_S = 300.0

# ── Target classification (drone detection) ──────────────────────────────────
DRONE_ALTITUDE_BOUNDS = [0, 500]  # metres ASL
DRONE_VELOCITY_BOUNDS = [-60, 60]  # m/s per component
DRONE_INITIAL_ALT_M = 80  # Solver initial guess altitude (m)
DRONE_MAX_SPEED_MS = 60.0  # Drone classification speed threshold (m/s)
DRONE_MAX_ALT_M = 600.0  # Drone classification altitude threshold (m)

# ── Frame processor cadences ─────────────────────────────────────────────────
ARC_REFRESH_S = 1.0  # Detection arc recompute cadence (s) — keep
# short so a deleted track's arc stops
# being broadcast quickly; otherwise the
# cached entry lingers up to this many
# seconds after the underlying detection
# ends, producing visible ghost arcs.
GT_REFRESH_S = 5.0  # Ground-truth snapshot refresh cadence (s)

# ── Periodic task intervals ──────────────────────────────────────────────────
REPUTATION_INTERVAL_S = 60  # Reputation evaluator sleep (s)
ADSB_TRUTH_INTERVAL_S = 120  # ADS-B truth fetcher sleep (s)
ADSB_BACKOFF_S = 300  # Rate-limit backoff (s)

# ── External ADS-B query regions ─────────────────────────────────────────────
ADSB_CELL_SPACING_KM = 400.0  # Lattice cell size for grouping nodes into queries
# Padding every query region carries around its members, sized against the
# fleet's configured max_range_km (140 at the widest).  An assumption about the
# fleet, not a bound from the contract: node_config accepts max_range_km up to
# 1000, and a node configured past this margin detects aircraft the query never
# asks for, so the truth fetcher logs the shortfall rather than under-covering
# it silently.  Raising this raises every box's area and so its OpenSky credits.
ADSB_NODE_RANGE_MARGIN_KM = 150.0
# A query must reach a node anywhere in its cell plus that node's detection
# range, so radius >= spacing/sqrt(2) + margin = 283 + 150 = 433 km = 234 nm.
# That is the ceiling; real groups are tighter because geometry comes from the
# members, not the cell.  Stays under adsb.lol's 250 nm schema cap.
ADSB_MAX_REGIONS_PER_CYCLE = 8  # adsb.lol's measured burst bucket

# How old either side of an ADS-B cross-validation may be.  Applied to the
# external entry and to the node's sample separately, so the worst skew between
# them is twice this: at 250 m/s that is 7.5 km of honest motion against a
# 10 km mismatch bar, which leaves the bar measuring disagreement rather than
# elapsed time.  Tight enough that the check only fires on samples captured near
# a poll — widening it means dead-reckoning the entry first, not a bigger number.
XVAL_MAX_AGE_S = 15.0
# How far behind us a node's frame timestamp may sit and still be believed.
# Backlog makes a frame genuinely older than its arrival, and reporting that is
# the point, so this is wide enough not to mistake backlog for a broken clock.
ADSB_CAPTURE_MAX_SKEW_S = 300.0
# How far ahead of us it may sit.  Tight, and deliberately not symmetric with
# the above: a stamp in the future is not stale to any gate and never becomes
# so, so only network and scheduling jitter is allowed for.
ADSB_CAPTURE_MAX_LEAD_S = 2.0
# How long an external ADS-B entry stays servable after its own capture time.
# Derived, not chosen: a rate-limited poll cycle is already
# ADSB_TRUTH_INTERVAL_S + ADSB_BACKOFF_S apart, so anything tighter would blank
# the fallback during ordinary backoff.  The margin covers fetch latency.  This
# is an upper bound on how long an outage can go on serving last-good data, not
# a freshness claim: entries carry their true age and every consumer gates on it.
EXTERNAL_ADSB_MAX_AGE_S = ADSB_TRUTH_INTERVAL_S + ADSB_BACKOFF_S + 180
# Oldest external entry a consumer scoring solves will treat as truth.  Much
# tighter than the retention bound above, which only says when the poller stops
# holding an entry at all: at 250 m/s a 120 s fix is 30 km from the aircraft,
# and verification reports the difference as node error.
EXTERNAL_TRUTH_MAX_AGE_S = 120.0

# ── Admin / ops ──────────────────────────────────────────────────────────────
EVENT_LOG_MAX = 2000  # Event log buffer capacity
NODE_OFFLINE_THRESHOLD_S = 120  # Heartbeat timeout → offline (s)
NODE_HEALTH_CHECK_INTERVAL_S = 30  # How often to check node liveness (s)
STORAGE_CACHE_TTL_S = 300.0  # Archive storage stats cache TTL (s)
CONFIG_LIVE_CACHE_TTL_S = 60.0  # Live node/tower config cache TTL (s)

# ── Chain of custody limits ──────────────────────────────────────────────────
CHAIN_ENTRIES_MAX_PER_NODE = 500  # Max chain entries per node (rolling)
IQ_COMMITMENTS_MAX_PER_NODE = 200  # Max IQ commitments per node (rolling)
RATE_BUCKETS_MAX_IPS = 10_000  # Max unique IPs in rate limiter

# ── blah2 bridge ─────────────────────────────────────────────────────────────
BLAH2_POLL_INTERVAL_S = 1.0  # blah2 API poll cadence (s)
BLAH2_STALE_THRESHOLD_S = 10.0  # Ignore frames older than this (s)
BLAH2_RECONNECT_DELAY_S = 5.0  # Backoff after failures (s)
BLAH2_MAX_FAILURES = 5  # Failures before backing off

# ── Node retirement ──────────────────────────────────────────────────────────
# Which node ids the admin route will force-retire.  Empty, the default,
# imposes no restriction, which is what production wants: a decommissioned
# receiver stays in connected_nodes until something retires it, so retiring a
# real node needs force=true too, and a prefix test would refuse the operator
# case the endpoint exists for.  Staging sets the test prefixes, because that
# is where the E2E suite force-retires its own nodes and where a mistaken sweep
# would cost something.


def force_retire_prefixes() -> tuple[str, ...]:
    """Node id prefixes that may be force-retired, read at call time.

    Not bound at import: main.py calls load_dotenv() only after the route
    imports, so an import-time read misses anything set in backend/.env and
    the guard would resolve to unrestricted without saying so.  Retirement is
    rare, so re-reading costs nothing.
    """
    return parse_comma_list(os.getenv("NODE_FORCE_RETIRE_PREFIXES", ""))


# ── Public location fuzzing (node privacy) ───────────────────────────────────
# A receiver sits at someone's home.  Everything below is about what an
# UNAUTHENTICATED client is told, and nothing else: services/public_location.py
# applies a deterministic per-node offset at the serialization boundary, while
# the pipeline (node_pipeline → solver → association) keeps using the true
# coordinates.  Fuzzing anything the physics reads would move the aircraft, not
# the operator.  TX sites are licensed broadcast towers, public by definition,
# and are never fuzzed — fuzzing them would only hide which tower a node uses
# while leaving the tower itself on every map.
#
# The offset is a "donut": a bearing uniform in [0, 360) and a displacement
# uniform in [NODE_FUZZ_MIN_KM, NODE_FUZZ_MAX_KM].  The minimum is what makes
# it a donut rather than a disc — a uniform disc puts a meaningful share of
# nodes within a few hundred metres of home, which is the case the fuzz exists
# to prevent.
#
# The bounds are also part of the HMAC message public_location.py hashes, so
# editing either one re-draws each node's bearing as well as its distance.
# Changing them is therefore a re-fuzz of the whole fleet — see that module's
# docstring for why anything less would hand the true receiver back to anyone
# holding a published position from both frames.
#
# These are read at call time, not bound at import, for the same reason as
# force_retire_prefixes() above: main.py calls load_dotenv() after the route
# imports, so an import-time read would silently miss a salt set in
# backend/.env and every node would move the day the file started being read.
NODE_FUZZ_MODE_DEFAULT = "on"  # "off" disables; anything else enables
# Narrowed from [1.0, 3.0] on 2026-09-05.  The wider donut put a node in the
# wrong part of the metro area, which read on the map as a receiver that could
# not be where its own coverage said it was, and drew a 3 km uncertainty disc
# over most of a city at the zooms people actually use.  0.5 km is still a
# floor of several hundred houses, and the honest limit of what a single fuzzed
# anchor buys is set by correlation across channels, not by this radius —
# services/public_location.py says so at length.
NODE_FUZZ_MIN_KM_DEFAULT = 0.5  # Inner radius of the donut (km)
NODE_FUZZ_MAX_KM_DEFAULT = 1.0  # Outer radius of the donut (km)


def node_fuzz_mode() -> str:
    """Whether public payloads carry fuzzed coords: "on" (default) or "off"."""
    return (os.getenv("NODE_FUZZ_MODE") or NODE_FUZZ_MODE_DEFAULT).strip().lower()


def node_fuzz_min_km() -> float:
    """Minimum public-location displacement (km); falls back on a bad value."""
    try:
        return float(os.getenv("NODE_FUZZ_MIN_KM") or NODE_FUZZ_MIN_KM_DEFAULT)
    except ValueError:
        return NODE_FUZZ_MIN_KM_DEFAULT


def node_fuzz_max_km() -> float:
    """Maximum public-location displacement (km); falls back on a bad value.

    Also what ``location_uncertainty_km`` reports to clients, so a deployment
    that widens the donut widens the honest uncertainty disc with it.
    """
    try:
        value = float(os.getenv("NODE_FUZZ_MAX_KM") or NODE_FUZZ_MAX_KM_DEFAULT)
    except ValueError:
        return NODE_FUZZ_MAX_KM_DEFAULT
    # A max below the min would invert the interval and silently produce
    # displacements outside both bounds.  Clamp rather than raise: a
    # misconfigured pair must not stop the server serving.
    return max(value, node_fuzz_min_km())


def node_fuzz_salt() -> str:
    """HMAC salt from the environment, or "" when the deployment sets none.

    An empty return means services/public_location.py falls back to the salt
    it persists under the runtime data dir, so offsets survive a restart
    without anyone having to configure anything.
    """
    return (os.getenv("NODE_FUZZ_SALT") or "").strip()
