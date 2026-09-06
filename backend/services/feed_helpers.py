"""Shared helpers for the aircraft feed: dedup, history, motion, identity.

Extracted from frame_processor.py (1553 lines, six responsibilities) — these
are the pure-ish helpers the feed builder, the track gates and the frame
processor all share.  They import only core state + geodesy, so every other
feed module can depend on them without cycles.
"""

import math
from collections import deque

from config.constants import ADSB_CAPTURE_MAX_LEAD_S, ADSB_CAPTURE_MAX_SKEW_S, is_num
from core import state
from services.geo import enu_km, haversine_km
from services.id_utils import normalize_hex_key

# Was a flat-earth approximation at 111.0 km/deg — 0.175% short of the
# spherical form used by every gate this feeds.
position_distance_km = haversine_km


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only.

    Currently a no-op: this module keeps no private mutable state (the
    arc-motion log lives on core.state).  Kept so conftest's autouse reset
    doesn't need to special-case the module.
    """


# ── Aircraft de-duplication ───────────────────────────────────────────────────
# Preference when several entries describe one aircraft. A multinode fix is
# constrained by >=2 receivers; a single-node ellipse arc is only an ambiguity
# locus, so it loses.  `adsb_single_node` sits second because its position IS
# the transponder's own fix rather than any estimate derived from one node's
# geometry — only a multi-receiver solve, which is independent of ADS-B
# altogether, outranks it.
_DEDUP_SOURCE_RANK = {
    "multinode_solve": 0,
    "adsb_single_node": 1,
    "solver_adsb_seed": 2,
    "solver_single_node": 3,
    "single_node_ellipse_arc": 4,
}
# Deliberately tighter than the 6 km solver association gate. Observed duplicate
# spread is ~1 km, while real IFR separation is 5 nm (~9.3 km) laterally or
# 1000 ft vertically — so this collapses duplicates with margin to spare while
# staying well clear of merging two genuinely separate aircraft. Under-merging
# leaves a cosmetic double; over-merging would hide real traffic.
_DEDUP_PROXIMITY_KM = 3.0
_DEDUP_ALT_GATE_FT = 2000.0


def adsb_capture_ts_ms(frame: dict, now_s: float) -> int:
    """When this frame's ADS-B positions were measured, in epoch ms.

    `frame["timestamp"]` is the end of the node's capture window, which is what
    every downstream staleness gate means by last_seen_ms.  Receipt time stands
    in where the frame carries no usable stamp, counted so a fleet drifting into
    the fallback is visible rather than silent.

    The bound is deliberately asymmetric.  A frame genuinely older than its
    arrival is the case this exists for, so the past is allowed the width of a
    real backlog; the future is allowed only clock jitter, because a stamp ahead
    of us is not stale to any gate at any point and never becomes so.
    """
    ts_ms = frame.get("timestamp")
    if is_num(ts_ms) and ts_ms > 0:
        offset_s = now_s - ts_ms / 1000
        if -ADSB_CAPTURE_MAX_LEAD_S <= offset_s <= ADSB_CAPTURE_MAX_SKEW_S:
            return int(ts_ms)
    state.bump_counter("adsb_capture_ts_fallback")
    return int(now_s * 1000)


def adsb_store(hex_code: str, rec: dict) -> bool:
    """Publish one ADS-B record, unless a newer fix for that hex is already in.

    last_seen_ms is a capture time now, so writes no longer arrive in stamp
    order: the TCP path stores every message immediately and stores the queued
    frame again a queue-latency later, and several nodes report one aircraft
    into a store keyed by hex alone.  Without this guard a replayed or
    backlogged frame walks the stamp backwards and feed_gc evicts an aircraft
    that is being reported right now.
    """
    existing = state.adsb_aircraft.get(hex_code)
    if existing is not None and existing.get("last_seen_ms", 0) > rec.get("last_seen_ms", 0):
        return False
    state.adsb_aircraft[hex_code] = rec
    return True


def _looks_like_same_aircraft(a: dict, b: dict) -> bool:
    """Proximity + altitude test for entries with no shared identity."""
    for k in ("lat", "lon"):
        if not isinstance(a.get(k), (int, float)) or not isinstance(b.get(k), (int, float)):
            return False
    if position_distance_km(a["lat"], a["lon"], b["lat"], b["lon"]) > _DEDUP_PROXIMITY_KM:
        return False
    alt_a, alt_b = a.get("alt_baro"), b.get("alt_baro")
    if isinstance(alt_a, (int, float)) and isinstance(alt_b, (int, float)):
        if abs(alt_a - alt_b) > _DEDUP_ALT_GATE_FT:
            return False
    return True


def _dedup_quality_key(entry: dict) -> tuple[int, float, float]:
    """Order entries inside a dedup group best-first.

    Source rank still decides first — a multi-receiver fix outranks anything
    derived from one node's geometry.  What follows it used to be nothing at
    all: the sort was stable and same-rank entries kept list order, which is
    insertion order in state.multinode_tracks, so the *oldest* key for a target
    won every tie.  Live that meant a stale n=2 solve, seconds old and
    kilometres off, beating a fresh 8-node fix for the same aircraft.  More
    receivers first, then the fresher fix; an entry that carries neither field
    sorts as if it had no nodes and had not been seen.
    """
    rank = _DEDUP_SOURCE_RANK.get(entry.get("position_source"), 99)
    n_nodes = entry.get("n_nodes")
    seen = entry.get("seen")
    return (
        rank,
        -(n_nodes if isinstance(n_nodes, (int, float)) else 0),
        seen if isinstance(seen, (int, float)) else math.inf,
    )


def dedup_aircraft(aircraft: list[dict]) -> list[dict]:
    """Collapse entries that describe the same physical aircraft.

    Two entries can describe one aircraft and never share a `hex`: a multinode
    solve is named mn<sha>, a single-node arc by ICAO or pr<id>. The `seen_hex`
    set in the builder is exact string equality, so it can never merge them —
    which is why one target could render as several icons.

    Grouped by ground_truth_hex where available, else by proximity. The
    ground-truth path is simulation-only (resolve_ground_truth_hex reads
    state.ground_truth_trails), so the proximity fallback is what carries real
    hardware.

    That resolver is a nearest-trail-end lookup in 2-D within 8 km, so it
    ignores altitude entirely and reaches far wider than the 3 km / 2000 ft
    test the proximity path applies.  It therefore hands one ground_truth_hex
    to aircraft that are plainly not the same target — measured live, a dark
    aircraft 1-2 km laterally from an airliner but 7,100 ft below it, whose own
    fresh 8-node solve then vanished under the airliner's entry.  So a
    multinode member of a ground-truth group that fails that proximity test
    against every entry kept so far is emitted on its own: two multi-receiver
    solves that far apart are two aircraft whatever the resolver said, and the
    resolver is an identity hint rather than evidence.  Lower-ranked members
    still collapse under the group's winner however far away they sit — an
    ellipse arc is a boresight crossing, not a position, and legitimately
    lands kilometres off the aircraft that produced it.
    """
    groups: list[list[dict]] = []
    # Parallel to `groups`: a proximity-formed group has already been tested
    # with the altitude gate, so only the ground-truth ones may need splitting.
    from_ground_truth: list[bool] = []
    by_truth: dict[str, int] = {}

    for ac in aircraft:
        gt = ac.get("ground_truth_hex")
        if gt:
            idx = by_truth.get(gt)
            if idx is None:
                by_truth[gt] = len(groups)
                groups.append([ac])
                from_ground_truth.append(True)
            else:
                groups[idx].append(ac)
            continue
        for g in groups:
            if _looks_like_same_aircraft(ac, g[0]):
                g.append(ac)
                break
        else:
            groups.append([ac])
            from_ground_truth.append(False)

    out: list[dict] = []
    for g, by_identity in zip(groups, from_ground_truth):
        if len(g) == 1:
            out.append(g[0])
            continue
        g.sort(key=_dedup_quality_key)
        # winners[i] survives; collapsed[i] is everything folded into it, which
        # is what decides whose nodes it inherits.
        winners: list[dict] = [g[0]]
        collapsed: list[list[dict]] = [[g[0]]]
        for m in g[1:]:
            if by_identity and m.get("position_source") == "multinode_solve":
                for winner, members in zip(winners, collapsed):
                    if _looks_like_same_aircraft(m, winner):
                        members.append(m)
                        break
                else:
                    winners.append(m)
                    collapsed.append([m])
                continue
            collapsed[0].append(m)

        for winner, members in zip(winners, collapsed):
            # Carry every contributing node onto the survivor so node filtering
            # (aircraft_flush.filter_payload_to_nodes) and the frontend's
            # node-highlight still see this aircraft under each node that saw
            # it — but only the nodes of the entries actually folded into it.
            nodes: list[str] = []
            for member in members:
                for nid in [member.get("node_id"), *(member.get("contributing_node_ids") or [])]:
                    if nid and nid not in nodes:
                        nodes.append(nid)
            if nodes:
                winner["contributing_node_ids"] = nodes
            out.append(winner)

    return out


def append_track_history(
    hex_code: str,
    lat: float,
    lon: float,
    alt_ft: float,
    ts: float,
    public_lat: float | None = None,
    public_lon: float | None = None,
):
    """Append a position to the rolling track histories for a hex.

    Two stores, written in lockstep: ``state.track_histories`` in the true
    frame for every internal consumer, and ``state.track_histories_public`` in
    the frame a client is served.  ``public_lat``/``public_lon`` default to the
    true values, so a caller that has no translation to apply — a multinode
    solve, an ADS-B fix, anything with fuzzing off — gets two identical stores
    without having to say so.

    The dedup runs on the TRUE position only, and decides for both.  Deduping
    each store separately would let them drift apart in length, and the served
    trail is read by index against nothing else; keeping one decision keeps
    index i the same emit in both.  It is also the right frame to judge in:
    "has this aircraft moved" is a question about the aircraft, and the
    published delta changes between frames for reasons that have nothing to do
    with the target.
    """
    if hex_code not in state.track_histories:
        state.track_histories[hex_code] = deque(maxlen=state.TRACK_HISTORY_MAX)
    if hex_code not in state.track_histories_public:
        state.track_histories_public[hex_code] = deque(maxlen=state.TRACK_HISTORY_MAX)
    hist = state.track_histories[hex_code]
    if hist:
        dlat = abs(hist[-1][0] - lat)
        dlon = abs(hist[-1][1] - lon)
        if dlat < 0.00005 and dlon < 0.00005:
            return
    alt_r = round(alt_ft, 0)
    ts_r = round(ts, 1)
    hist.append([round(lat, 6), round(lon, 6), alt_r, ts_r])
    state.track_histories_public[hex_code].append(
        [
            round(lat if public_lat is None else public_lat, 6),
            round(lon if public_lon is None else public_lon, 6),
            alt_r,
            ts_r,
        ]
    )


# Minimum displacement (metres) before a new arc-motion sample is appended.
# Smaller than this counts as "same position" — sub-100 m drift on a stationary
# arc midpoint isn't a real velocity signal.
_ARC_MOTION_MIN_M = 100.0
# Number of distinct positions retained per track.  Long enough to span a few
# bistatic frames (~40 s apart) so velocity estimation can average over
# 60–120 s windows without holding state forever.
_ARC_MOTION_LOG_MAX = 6


def _record_arc_motion(ac_hex: str, lat: float, lon: float, ts: float) -> None:
    """Log distinct emitted positions for later velocity estimation.

    Only entries that have moved > _ARC_MOTION_MIN_M from the last logged
    position are appended — repeated emits at the same arc midpoint between
    detections would otherwise drown out real motion.
    """
    log = state.track_arc_motion.get(ac_hex)
    if log:
        plat, plon, _pts = log[-1]
        if haversine_km(lat, lon, plat, plon) * 1000.0 < _ARC_MOTION_MIN_M:
            return
    else:
        log = []
        state.track_arc_motion[ac_hex] = log
    log.append((lat, lon, ts))
    if len(log) > _ARC_MOTION_LOG_MAX:
        log.pop(0)


def _estimate_velocity_ms_from_motion(
    ac_hex: str,
    lat: float,
    lon: float,
    now: float,
) -> tuple[float, float] | None:
    """Return (vel_east_ms, vel_north_ms) inferred from arc-midpoint motion.

    Searches the per-track motion log for the oldest sample 15–120 s old
    that's > 200 m from the current position; uses that displacement to
    derive an averaged east/north velocity.  Returns None if no sample is
    recent-enough-but-old-enough or the displacement is too small to trust.

    No upper speed bound: supersonic targets are in scope, so a fast
    displacement is reported as-is rather than rejected as implausible.
    """
    log = state.track_arc_motion.get(ac_hex)
    if not log:
        return None
    for plat, plon, pts in log:
        dt = now - pts
        if dt < 15:
            continue  # too recent — noise dominates
        if dt > 120:
            continue  # too old — aircraft may have manoeuvred
        east_m, north_m = (c * 1000.0 for c in enu_km(plat, plon, lat, lon))
        dist_m = math.hypot(east_m, north_m)
        if dist_m < 200:
            continue  # essentially still — can't infer velocity
        return east_m / dt, north_m / dt
    return None


def _estimate_velocity_from_motion(
    ac_hex: str,
    lat: float,
    lon: float,
    now: float,
) -> tuple[float, float] | None:
    """Return (gs_knots, track_deg) inferred from arc-midpoint motion, or None.

    Thin wrapper over _estimate_velocity_ms_from_motion that converts to the
    knots/degrees form used by the emitted aircraft entry.
    """
    ms = _estimate_velocity_ms_from_motion(ac_hex, lat, lon, now)
    if ms is None:
        return None
    vel_e_ms, vel_n_ms = ms
    gs_kn = math.hypot(vel_e_ms, vel_n_ms) * 1.94384
    bearing_deg = math.degrees(math.atan2(vel_e_ms, vel_n_ms)) % 360.0
    return round(gs_kn, 1), round(bearing_deg, 1)


def resolve_ground_truth_hex(
    ac_hex: str,
    lat: float,
    lon: float,
    max_distance_km: float = 8.0,
) -> str | None:
    """Find the best ground-truth hex for a solved aircraft."""
    norm_hex = normalize_hex_key(ac_hex)
    if norm_hex and norm_hex in state.ground_truth_trails and state.ground_truth_trails[norm_hex]:
        return norm_hex

    best_hex = None
    best_distance = max_distance_km
    for gt_hex, trail in list(state.ground_truth_trails.items()):
        if not trail:
            continue
        last = trail[-1]
        dist = position_distance_km(lat, lon, last[0], last[1])
        if dist <= best_distance:
            best_distance = dist
            best_hex = gt_hex
    return best_hex
