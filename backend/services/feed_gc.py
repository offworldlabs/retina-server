"""Stale-store garbage collection for the aircraft feed.

Extracted from build_combined_aircraft_json section 4/4b — pure GC with no
output.  Bounds memory and keeps resolve_ground_truth_hex's O(N) scans cheap.

These run on their own timer (services.tasks.feed_gc_task), NOT off the back
of the feed build: the build only happens when the flush task gets round to
it, and the flush task can be held for seconds behind a websocket broadcast or
skipped entirely while state.aircraft_dirty is False — while frame workers and
solver threads keep writing every store below.
"""

from config.constants import GT_DISPLAY_STALE_S, KNOWN_CLAIMS_STALE_S, MN_DARK_EXPIRY_S, TRAIL_STALE_S
from core import state
from services.id_utils import multinode_hex_from_key

# Key prefix marking a transponder-anchored (assisted) multinode entry; every
# other key is a dark solve.  Lives here rather than in aircraft_feed because
# the expiry that reads it does, and aircraft_feed imports this module.
MN_ADSB_PREFIX = "mn-adsb-"

# Assisted entries keep the historic 60 s: the entry is anchored to a
# transponder hex, so a long gap is the ADS-B feed breathing.  A dark entry has
# nothing holding it in place — at the current 1-3 s dark solve cadence a
# MN_DARK_EXPIRY_S gap is a lost track, and the 30-60 s band measured 3.99 km
# median error (32% over 5 km): a confident icon kilometres from any aircraft.
MN_ASSISTED_EXPIRY_S = 60.0


def prune_stale_stores(now: float) -> None:
    # 4. ADS-B only — excluded from map per design.
    # Aircraft must have at least one radar detection to appear.
    # ADS-B data is used only as a solver seed and for enrichment
    # (callsign, altitude, velocity) of radar-detected aircraft.
    # Stale entries are still pruned to avoid unbounded memory growth.
    stale_adsb = []
    for hex_code, entry in list(state.adsb_aircraft.items()):
        age_s = now - entry.get("last_seen_ms", 0) / 1000
        if age_s > 60:
            stale_adsb.append(hex_code)
    for k in stale_adsb:
        state.adsb_aircraft.pop(k, None)

    # 4a. Known-lane claims: a hex not claimed within KNOWN_CLAIMS_STALE_S
    # has left coverage or lost its feed — either way the adsb_aircraft
    # aging above guarantees no new claim can form, so only the bounded
    # residual-history window (see the constant's rationale) keeps it here.
    stale_claims = [
        h
        for h, dq in list(state.known_claims.items())
        if not dq or (now - dq[-1]["ts_ms"] / 1000.0) > KNOWN_CLAIMS_STALE_S
    ]
    for h in stale_claims:
        state.known_claims.pop(h, None)

    # 4b. Prune stale ground-truth trails and track histories to bound
    # memory and keep resolve_ground_truth_hex O(N) scans cheap.
    #
    # These two used to share one 300 s constant, but they want opposite
    # things.  A ground-truth entry is a *current position* pushed every 2 s;
    # once the pushes stop the aircraft has despawned, and holding it for
    # 300 s paints a stationary blue dot on the map for five minutes.  A track
    # history is the *trail behind* an aircraft, where a long tail is wanted.
    stale_gt = [
        h
        for h, trail in list(state.ground_truth_trails.items())
        if not trail or (now - trail[-1][3]) > GT_DISPLAY_STALE_S
    ]
    for h in stale_gt:
        state.ground_truth_trails.pop(h, None)
        state.ground_truth_meta.pop(h, None)
        with state.anomaly_lock:
            state.anomaly_hexes.discard(h)

    stale_th = [
        h for h, trail in list(state.track_histories.items()) if not trail or (now - trail[-1][3]) > TRAIL_STALE_S
    ]
    for h in stale_th:
        state.track_histories.pop(h, None)
        # The public-frame twin ages out on the true store's verdict, not its
        # own: the two are appended in lockstep, so a separate staleness test
        # would only ever be a chance to disagree — and a public trail that
        # outlived its true counterpart would keep being served for a hex
        # nothing else in the process still knows about.
        state.track_histories_public.pop(h, None)
        # NOTE: do NOT prune track_last_emit on *this* verdict.
        # track_histories ages out via the ~5 m dedup even when the track is
        # still actively emitting the same arc midpoint.  Clearing the
        # speed-gate reference would then let the next bad measurement leak
        # through unchecked.  track_last_emit has its own age sweep below,
        # keyed on its own timestamp rather than the history's.

    # Arc-motion logs: also swept by the stale_geo cleanup, but tracks that
    # never enter active_geo_aircraft (default-pipeline path) would leak one
    # capped list per hex for the process lifetime — the dict itself had no
    # eviction anywhere.
    stale_motion = [
        h for h, log in list(state.track_arc_motion.items()) if not log or (now - log[-1][2]) > TRAIL_STALE_S
    ]
    for h in stale_motion:
        state.track_arc_motion.pop(h, None)

    # Speed-gate references and gate holds: same bug class as the arc-motion
    # log above, and written on the same line-block.  track_entry() runs from
    # two places — the active_geo_aircraft sweep, whose stale pass pops all
    # three, and the default-pipeline branch, which pops none.  A hex that
    # only ever reaches the feed on the second path (nodes registered without
    # rx_lat/tx_lat, HTTP/sim ingest, and every per-track-id pr* hex, which
    # never repeats) left an entry no code path could remove.
    #
    # TRAIL_STALE_S (300 s) is far longer than the 60 s window the speed gate
    # itself will act on (track_gates: `if 0 < _dt < 60`) and than
    # GATE_MAX_HOLD_S, so nothing here can weaken either gate: every entry
    # dropped is one both gates would already ignore.  The two are swept
    # together because the hold is meaningless without the reference it
    # reverts to, and the hold carries its anchor timestamp at v[0] against
    # last_emit's v[2].
    stale_emit = [h for h, v in list(state.track_last_emit.items()) if not v or (now - v[2]) > TRAIL_STALE_S]
    for h in stale_emit:
        state.track_last_emit.pop(h, None)
        state.track_gate_hold.pop(h, None)
    stale_hold = [h for h, v in list(state.track_gate_hold.items()) if not v or (now - v[0]) > TRAIL_STALE_S]
    for h in stale_hold:
        state.track_gate_hold.pop(h, None)


def prune_multinode_tracks(now: float) -> None:
    """Age out state.multinode_tracks, discarding each entry's anomaly hex.

    Snapshot and evict under the solver's track lock: the solver worker
    iterates state.multinode_tracks inside multinode_key_decision while holding
    it, and a pop from another thread mid-iteration raised "dictionary changed
    size during iteration" live (2026-09-05).  Lazy import: solver.py owns the
    lock and importing it at module level here would create a cycle through the
    task modules.

    This is the ONLY pruner of the store the solver writes at full rate.  It
    used to live inside the feed build, which meant a stalled flush — or a feed
    build skipped because nothing marked the feed dirty — left it growing
    unbounded.  It is idempotent, so the feed build still calls it before
    reading its snapshot and gets a store with nothing expired in it.
    """
    from services.tasks import solver as _solver_mod

    with _solver_mod._MN_TRACKS_LOCK:
        snapshot = list(state.multinode_tracks.items())
    stale = []
    for key, r in snapshot:
        age_s = now - r.get("timestamp_ms", 0) / 1000
        expiry_s = MN_ASSISTED_EXPIRY_S if key.startswith(MN_ADSB_PREFIX) else MN_DARK_EXPIRY_S
        if age_s > expiry_s:
            stale.append(key)
    for k in stale:
        # Must use the same derivation as insertion (multinode_to_aircraft ->
        # multinode_hex_from_key).  This previously used an obsolete 4-char
        # format that never matched the mn<sha256[:10]> actually inserted, so
        # no multinode anomaly hex was ever evicted and anomaly_hexes grew
        # without bound — enough to trip the anomaly_flood health check.
        with state.anomaly_lock:
            state.anomaly_hexes.discard(multinode_hex_from_key(k))
        with _solver_mod._MN_TRACKS_LOCK:
            state.multinode_tracks.pop(k, None)
