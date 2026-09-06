"""Dark track following (DARK_FOLLOW_MODE): the pseudo-state store behind the
top-down claiming of aircraft that have no transponder.

The dark lane is bottom-up all the way down — per-node tracker tracks, delay
grid pairing, clustering, solve, and only THEN a key, chosen by proximity to
whatever multinode entry happens to be nearest (solver.multinode_key_decision).
Two consequences the map shows directly.  Continuity: consecutive solves of one
aircraft less than 5 s apart land on a different key 15% of the time, any gap
in solving re-mints the key from scratch, and two aircraft 3 km apart can share
one.  Accuracy: nothing ever tells the solver where the aircraft is EXPECTED to
be, so a solve starts from a 3 km grid centroid.

The known lane already solved both problems for ADS-B aircraft, by inverting
the order: identity first (services/known_claiming.py claims detections against
a dead-reckoned transponder fix), then a solve seeded with that fix.  This
module supplies the missing half of the analogy — an identity for a dark
aircraft.  There is one available and it is already good: an established
multinode track with a Kalman state.  Its dead-reckoned position and velocity
predict (delay, Doppler) at any node exactly the way an ADS-B fix does, so the
same claiming machinery works against it unchanged.

WHY NOT NODE-TRACK IDS.  The obvious alternative — carry ``source_track_ids``
forward and attach each new solve to the newest key sharing a node-track id —
was simulated against a dense metro cluster and linked the WRONG aircraft 12%
of the time.  Single-node tracker tracks are genuinely shared between the
association candidates of different aircraft (the same measurement that made
solver._supersession_match stop trusting a bare shared id), so a node-track id
is evidence about a detection, not about an aircraft.  A predicted observation
is evidence about an aircraft, which is what keying needs.

THE GHOST RISK, AND THE GUARD.  Following a track is a positive feedback loop:
a solve keeps a key alive, the key keeps predicting, the prediction keeps
claiming detections.  Left alone that locks a ghost onto the map forever — the
bottom-up lane can no longer disagree with it, because binding mode takes the
detections away before the lane sees them.  So a followed key is dropped (and
put in cooldown, letting the bottom-up lane re-find it or not) as soon as it
stops earning its place: two rejected follow-solves in a row, or a filter
velocity sigma past DARK_FOLLOW_MAX_VEL_SIGMA_MS.  The guard is the reason this
lane is safe to bind; it is not optional tidiness.

Modes (state.DARK_FOLLOW_MODE), same three-way shape as KNOWN_LANE_MODE:
  off      — nothing; no targets are built, so no claim can form.
  shadow   — claim, solve and record, but the claimed detections stay in the
             dark pool and nothing is published.  Default: the lane changes
             which aircraft the map believes in, and that earns a soak.
  binding  — claimed detections leave the dark pool (the same
             strip_claimed_detections the known lane uses) and the solve goes
             onto the normal solver queue, keyed onto the followed track by
             its anchor.  Binding also gives the lane OWNERSHIP of the keys it
             follows: a bottom-up solve may not join a key this lane published
             on in the last DARK_FOLLOW_OWN_S — see that constant for the
             measurement, and recently_followed for the reader.
"""

import logging
import os
import threading
import time

from config.constants import C_KM_US
from core import state
from services import track_filter

_logger = logging.getLogger(__name__)

# ── Target eligibility ───────────────────────────────────────────────────────
# How stale a dark track's last solve may be and still be followed.  Shorter
# than the map's own 60 s expiry on purpose: past ~20 s the dead-reckoned
# position is the KF's extrapolation rather than a measurement, and a claim
# made against it would be the lane inventing its own evidence.
DARK_FOLLOW_MAX_AGE_S = float(os.getenv("DARK_FOLLOW_MAX_AGE_S", "20"))
# Minimum solves before a key may be followed.  A key with one or two solves
# behind it is exactly what the bottom-up lane mints for a mis-associated
# fragment; requiring three means the aircraft has survived the whole gate
# stack repeatedly before anything is claimed on its behalf.  Same intent as
# the library's CLAIM_ELIGIBLE_MIN_SOLVE_COUNT, one notch stricter because a
# claim here also REMOVES detections from the lane that would disagree.
DARK_FOLLOW_MIN_SOLVES = int(os.getenv("DARK_FOLLOW_MIN_SOLVES", "3"))
# ...and how many nodes the last solve used.  n>=3 is where the multinode
# position is overdetermined; an n=2 track is a bistatic intersection that the
# displacement and beam gates are still arguing about.
DARK_FOLLOW_MIN_NODES = 3
# Velocity sigma ceiling.  The KF's own statement about how well it knows the
# track's velocity, and the term that dominates the prediction error the moment
# the track is coasted: at 60 m/s a 2 s coast is already 120 m of position
# uncertainty, and the Doppler allowance the gate below derives from it is
# ~78 Hz against a 25 Hz base — wider than the base gate, which is the point
# past which the "prediction" stops constraining anything.
DARK_FOLLOW_MAX_VEL_SIGMA_MS = float(os.getenv("DARK_FOLLOW_MAX_VEL_SIGMA_MS", "60"))
# How long a dropped key stays un-followable.  Long enough that the bottom-up
# lane gets several association rounds (ASSOC_MIN_INTERVAL_S is 30 s at its
# widest, ~2 s at its narrowest) to re-find the aircraft on its own evidence
# before this lane is allowed to assert it again.
DARK_FOLLOW_COOLDOWN_S = float(os.getenv("DARK_FOLLOW_COOLDOWN_S", "30"))
# Minimum spacing between follow-solves for one key.  Matched to the known
# lane's pass interval: the aircraft is already being solved bottom-up too, and
# a follow-solve every 2 s is four refreshes inside the map's 60 s expiry.
DARK_FOLLOW_INTERVAL_S = float(os.getenv("DARK_FOLLOW_INTERVAL_S", "2.0"))

# ── Key ownership ────────────────────────────────────────────────────────────
# How long after a follow-solve publishes on a key that key stays the follow
# lane's, i.e. un-joinable by a bottom-up solve (solver.multinode_key_decision,
# binding mode only).  Three follow-solve intervals: a followed track is solved
# every DARK_FOLLOW_INTERVAL_S, so a key still inside this window is one the
# lane is actively refreshing and does not need help keeping alive, while a key
# that has missed three turns is one the lane has stopped answering for and the
# bottom-up lane should be free to claim again.
#
# WHY OWNERSHIP AT ALL.  Measured on test with the lane binding (20 min, 625
# six-plus-node dark samples): of 425 bottom-up solves keyed by proximity onto
# an existing key, 90 landed on a key belonging to a DIFFERENT aircraft (21%),
# 12 of them onto a key the follow lane had published on within the previous
# 6 s.  A cross-keyed solve moves the entry 5+ km, corrupts the KF velocity it
# feeds, and can supersede the right key.  A tighter spatial gate cannot
# separate the two cases: same-aircraft re-key distances are p50 1.5 km /
# p90 4.3 km, overlapping the wrong-aircraft population entirely.  What CAN
# separate them is that the follow lane already supplies every solve an
# established track needs — so near a freshly-followed key a bottom-up solve is
# either a duplicate (it competes with the anchored solve and drags the filter)
# or a different aircraft (it steals the key).  Neither should join.
DARK_FOLLOW_OWN_S = float(os.getenv("DARK_FOLLOW_OWN_S", "6.0"))
# How close a bottom-up solve has to land to a followed key before it is
# refused outright rather than merely kept off that key.  Inside this radius
# the two are the same aircraft often enough that minting a second key would
# just fragment the track; outside it the solve is plausibly a neighbour the
# follow lane knows nothing about and deserves a key of its own.  Well under
# the 6 km proximity gate on purpose — this is a "these are the same target"
# radius, not an association gate.
DARK_FOLLOW_SHADOW_KM = float(os.getenv("DARK_FOLLOW_SHADOW_KM", "2.0"))

# Consecutive rejected follow-solves that drop a key.  Two, not one: a single
# reject is routinely a bad epoch (one node's contaminated measurement trips
# the rms gate), while two in a row is the prediction itself being wrong.
_MAX_CONSECUTIVE_REJECTS = 2

# Position sigma to assume for a followed entry whose record carries none —
# the KF's own cold-start value (track_filter._KF_DEFAULT_POS_SIGMA_M).  An
# entry without a filter-reported sigma is one the smoother has not updated,
# so the cold-start number is the honest statement about it; deliberately not
# a tighter guess, because understating sigma here narrows a gate.
_DEFAULT_POS_SIGMA_M = 1200.0

# Ceilings on the widened gates (see follow_gates).  The uncertainty terms are
# bounded by the thresholds above in normal operation, but the filter's own
# sigma cap is 8 km, which alone would open a 53 µs delay gate — at that width
# the claim is no longer a prediction test.  4x the base gates: past there the
# aircraft should be re-found bottom-up, not followed.
_MAX_DELAY_GATE_US = 40.0
_MAX_DOPPLER_GATE_HZ = 100.0

# Metres per microsecond and metres per second, from the one speed-of-light
# constant this repo has.
_C_M_PER_US = C_KM_US * 1000.0
_C_M_PER_S = C_KM_US * 1.0e9

# ── Target cache ─────────────────────────────────────────────────────────────
# Rebuilt at most this often.  The claiming stage runs once per frame per node
# — ~37 calls/s on the test fleet — and each rebuild walks multinode_tracks and
# takes track_filter's lock once per dark key.  One second is short against
# DARK_FOLLOW_MAX_AGE_S (so a target never survives its own staleness by more
# than a rebuild) and long enough that the walk is amortised ~37x.  The
# per-frame dead-reckoning happens at the CALL site against the frame's own
# timestamp, so caching the base list costs no prediction accuracy.
_TARGETS_TTL_S = 1.0

_TARGETS_LOCK = threading.Lock()
_targets: list[dict] = []
_targets_built_mono = 0.0

# Guard state: consecutive rejects per key, and the cooldown each drop starts.
_GUARD_LOCK = threading.Lock()
_reject_streak: dict[str, int] = {}
_cooldown_until: dict[str, float] = {}

# Ownership state: key → the measurement epoch of the newest follow-solve that
# published on it (see note_follow_publish for why the measurement clock and
# not wall time).  Dict-level TTL, same shape and reason as the guard maps
# above: mn-dark-* keys churn for the process lifetime, so nothing keyed by one
# may grow unbounded.  60 s is the map's own expiry — a key with no follow
# publish for that long has no entry left to own.
_FOLLOWED_TTL_S = 60.0
_FOLLOWED_LOCK = threading.Lock()
_last_follow_publish: dict[str, float] = {}


def _reset_for_tests() -> None:
    """Drop the target cache and the guard state.  Tests only."""
    global _targets, _targets_built_mono
    with _TARGETS_LOCK:
        _targets = []
        _targets_built_mono = 0.0
    with _GUARD_LOCK:
        _reject_streak.clear()
        _cooldown_until.clear()
    with _FOLLOWED_LOCK:
        _last_follow_publish.clear()
    state.dark_follow_targets = 0


def _expire_targets_for_tests() -> None:
    """Force the next follow_targets() call to rebuild, KEEPING the guard state.

    Tests only, and distinct from _reset_for_tests for exactly that reason: the
    guard's whole behaviour is "a key that was a target stops being one", which
    is unobservable if the only way to re-read the list also forgets the drop.
    """
    global _targets_built_mono
    with _TARGETS_LOCK:
        _targets_built_mono = 0.0


def mode() -> str:
    """The follow mode, defensively — the known lane's ``_mode`` precedent.

    An absent or unrecognised value is "off": a flag this module cannot read
    must disable the lane, never bind it.
    """
    m = getattr(state, "DARK_FOLLOW_MODE", "off")
    return m if m in ("off", "shadow", "binding") else "off"


def drop_target(key: str, reason: str) -> None:
    """Stop following ``key`` for DARK_FOLLOW_COOLDOWN_S.

    Idempotent inside a cooldown window: a key already in cooldown is not
    re-dropped, so the counter reads "keys dropped", not "times the drop
    condition was re-observed".  The target cache is not invalidated — it
    expires within _TARGETS_TTL_S, and the cooldown is re-tested on rebuild.
    """
    now = time.monotonic()
    with _GUARD_LOCK:
        if _cooldown_until.get(key, 0.0) > now:
            return
        _cooldown_until[key] = now + DARK_FOLLOW_COOLDOWN_S
        _reject_streak.pop(key, None)
    state.bump_counter("dark_follow_dropped")
    _logger.debug("dark-follow: dropped %s (%s)", key, reason)


def record_outcome(key: str, ok: bool) -> None:
    """Feed one follow-solve verdict to the ghost guard.

    ``ok`` is "this solve reached the feed" in binding mode and "this solve
    landed within the dark displacement cap of its own prediction" in shadow —
    the shadow pass has no gates to be rejected by, and a guard that stayed
    inert there would leave the soak measuring a lane the binding one does not
    have.  Either way a good solve clears the streak and
    _MAX_CONSECUTIVE_REJECTS bad ones in a row drop the key.
    """
    if ok:
        with _GUARD_LOCK:
            _reject_streak.pop(key, None)
        return
    with _GUARD_LOCK:
        streak = _reject_streak.get(key, 0) + 1
        _reject_streak[key] = streak
    if streak >= _MAX_CONSECUTIVE_REJECTS:
        drop_target(key, f"{streak} consecutive rejected follow-solves")


def note_follow_publish(key: str, ts_s: float) -> None:
    """Record that a follow-lane solve published on ``key`` at epoch ``ts_s``.

    ``ts_s`` is the solve's MEASUREMENT epoch, not wall time, and that is not
    an accident: the only reader is solver.multinode_key_decision, which is
    deliberately clock-free — every time-of-day it uses arrives on the solve it
    is judging — so that the keying rule stays replayable against recorded
    history.  Feeding it wall time here would make the one gate that decides
    key ownership the one thing a replay could not reproduce.

    Called from the single point every follow-solve outcome passes through
    (solver._record_solve_history), so there is no path that publishes on a
    followed key without marking it.
    """
    if not key or ts_s <= 0.0:
        return
    with _FOLLOWED_LOCK:
        _last_follow_publish[key] = ts_s
        cutoff = ts_s - _FOLLOWED_TTL_S
        for k in [k for k, t in _last_follow_publish.items() if t < cutoff]:
            del _last_follow_publish[k]


def recently_followed(key: str, now_s: float, within_s: float = DARK_FOLLOW_OWN_S) -> bool:
    """Did the follow lane publish on ``key`` within ``within_s`` of ``now_s``?

    Symmetric in time on purpose.  Solves reach the worker out of order (three
    worker threads, a queue, and a per-key rate limit that batches claims), so
    a bottom-up solve whose epoch sits just BEFORE the follow-solve's is
    looking at the same instant of the same aircraft as one just after it, and
    the ownership answer has to be the same for both.
    """
    with _FOLLOWED_LOCK:
        last = _last_follow_publish.get(key)
    return last is not None and abs(now_s - last) <= within_s


def _in_cooldown(key: str, now_mono: float) -> bool:
    with _GUARD_LOCK:
        return _cooldown_until.get(key, 0.0) > now_mono


def _sweep_guard(now_mono: float) -> None:
    """Drop expired cooldowns.  Both maps are keyed by mn-dark-* ids, which
    churn for the process lifetime, so neither may grow unbounded."""
    with _GUARD_LOCK:
        for k in [k for k, until in _cooldown_until.items() if until <= now_mono]:
            del _cooldown_until[k]
            _reject_streak.pop(k, None)


def _pos_sigma_m(rec: dict) -> float:
    """The followed entry's own position uncertainty, in metres.

    ``kf_pos_sigma_m`` is the smoother's post-update marginal (the honest
    number, and the one solve_uncertainty already calibrates against);
    ``pos_sigma_km`` is the solver's formal pre-inflation sigma, used only when
    the filter never ran on this key.  Both absent means a cold entry, which
    gets the filter's own cold-start sigma rather than a flattering guess.
    """
    kf = rec.get("kf_pos_sigma_m")
    if isinstance(kf, (int, float)) and kf > 0:
        return float(kf)
    formal = rec.get("pos_sigma_km")
    if isinstance(formal, (int, float)) and formal > 0:
        return float(formal) * 1000.0
    return _DEFAULT_POS_SIGMA_M


def _build_targets(now_s: float, now_mono: float) -> list[dict]:
    """One pseudo-state per followable dark track.  Cheap, and cached."""
    out: list[dict] = []
    for key, rec in list(state.multinode_tracks.items()):
        if not key.startswith("mn-dark-"):
            continue
        if _in_cooldown(key, now_mono):
            continue
        lat, lon = rec.get("lat"), rec.get("lon")
        if lat is None or lon is None:
            continue
        ts_ms = rec.get("timestamp_ms") or 0
        age_s = now_s - ts_ms / 1000.0
        if not (0.0 <= age_s <= DARK_FOLLOW_MAX_AGE_S):
            continue
        if int(rec.get("solve_count") or 0) < DARK_FOLLOW_MIN_SOLVES:
            continue
        if int(rec.get("n_nodes") or 0) < DARK_FOLLOW_MIN_NODES:
            continue
        # No filter state, no follow.  The velocity and its sigma are the whole
        # prediction: without them there is nothing to dead-reckon with and no
        # way to widen a gate honestly, and the raw solved velocity is exactly
        # the under-determined quantity (n<=3 Doppler) the KF exists to fix.
        lv = track_filter.learned_velocity(key)
        if lv is None:
            continue
        vel_east, vel_north, vel_sigma_ms, _last_ts_s = lv
        if vel_sigma_ms > DARK_FOLLOW_MAX_VEL_SIGMA_MS:
            drop_target(key, f"velocity sigma {vel_sigma_ms:.0f} m/s")
            continue
        # World tag.  The overlap-zone world gate means every node that
        # contributed to one multinode entry is from a single world, so the
        # first contributor answers for all of them; an entry with no
        # contributor list left (an old record shape) is untagged, and untagged
        # passes every world — the same rule known_claiming's ADS-B world gate
        # applies to a cache entry written before worlds existed.
        node_ids = list(rec.get("contributing_node_ids") or ())
        world = state.node_world(node_ids[0]) if node_ids else None
        out.append(
            {
                "key": key,
                "lat": float(lat),
                "lon": float(lon),
                "alt_m": float(rec.get("alt_m") or 0.0),
                "vel_east": float(vel_east),
                "vel_north": float(vel_north),
                "vel_sigma_ms": float(vel_sigma_ms),
                "pos_sigma_m": _pos_sigma_m(rec),
                "timestamp_ms": int(ts_ms),
                "world": world,
            }
        )
    return out


def follow_targets() -> list[dict]:
    """The current dark follow targets, rebuilt at most once per _TARGETS_TTL_S.

    Returns the shared cached list; callers must treat it (and the dicts in it)
    as read-only.  Empty in ``off`` mode, which is what makes the whole lane
    cost one attribute read per frame when it is switched off.
    """
    global _targets, _targets_built_mono
    if mode() == "off":
        return []
    now_mono = time.monotonic()
    with _TARGETS_LOCK:
        if _targets_built_mono and now_mono - _targets_built_mono < _TARGETS_TTL_S:
            return _targets
        _sweep_guard(now_mono)
        _targets = _build_targets(time.time(), now_mono)
        _targets_built_mono = now_mono
        # A gauge, not a counter: it is the size of the list above, so it is
        # assigned rather than bumped (state.bump_counter only adds).
        state.dark_follow_targets = len(_targets)
        return _targets


def follow_gates(
    target: dict, dt_s: float, base_delay_us: float, base_doppler_hz: float, fc_hz: float
) -> tuple[float, float]:
    """Claim gates for one follow target coasted ``dt_s`` seconds, in
    (delay µs, Doppler Hz).

    Two terms, because there are two independent error sources and the ADS-B
    path only has the first:

      MEASUREMENT.  ``base * _gate_scale(dt)`` — the known lane's own gate,
      age-scaled exactly as it is there (services/known_claiming._gate_scale).
      This covers detection noise and the node's own bias.

      STATE.  The followed track's own uncertainty, converted into observation
      space.  A position error of ``s`` metres moves the bistatic range by at
      most ``2s`` (the target can be displaced toward both the transmitter and
      the receiver), so it is worth ``2s / c`` microseconds of delay; the
      position error at claim time is the filter's position sigma plus its
      velocity sigma coasted over ``dt``.  A velocity error of ``u`` m/s
      likewise moves the bistatic Doppler by at most ``2u / λ`` = ``2u·fc / c``
      hertz.  Both are worst-case projections (the true geometry factor is a
      cosine ≤ 1), which is the right direction for a gate: it may admit a
      detection the geometry would have excluded, never exclude one it should
      have admitted.

    Capped at _MAX_DELAY_GATE_US / _MAX_DOPPLER_GATE_HZ — see those constants.
    """
    # Imported here rather than at module scope: known_claiming imports this
    # module for the claiming path, and a top-level import back would be a
    # cycle.  _gate_scale is the age-scaling rule itself, and duplicating it
    # would let the two lanes drift apart on the same physics.
    from services.known_claiming import _gate_scale

    scale = _gate_scale(dt_s)
    pos_err_m = target["pos_sigma_m"] + target["vel_sigma_ms"] * max(dt_s, 0.0)
    d_gate = base_delay_us * scale + 2.0 * pos_err_m / _C_M_PER_US
    f_gate = base_doppler_hz * scale + 2.0 * target["vel_sigma_ms"] * fc_hz / _C_M_PER_S
    return min(d_gate, _MAX_DELAY_GATE_US), min(f_gate, _MAX_DOPPLER_GATE_HZ)
