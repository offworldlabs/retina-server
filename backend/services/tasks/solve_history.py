"""Per-solve history: the record every solver outcome leaves behind.

The write side of the solve-history deques in core.state, whose read side is
services.solver_report.  Nothing in the solver's gate chain reads what these
functions return.
"""

import math
import time

from retina_analytics.association import _point_in_beam

from core import state
from services import dark_follow
from services.geo import bearing_deg, offset_latlon_m
from services.geo import haversine_km as _haversine_km
from services.id_utils import multinode_hex_from_key, normalize_hex_key
from services.solve_uncertainty import solve_sigma_m
from services.tasks import displacement_caps

# ── Per-solve history (debug) ────────────────────────────────────────────────
# Every solver outcome — published or gate-rejected — is appended to
# state.mlat_solve_history so /api/test/mlat-history can decompose a bad map
# marker into raw solves, smoothing, dead-reckoning, GT binding and gate
# rejections after the fact.  Retention ~30 min (age-pruned on write, hard
# capped by the deque's maxlen).
_MLAT_HISTORY_MAX_AGE_MS = 35 * 60 * 1000
# GT trail points are pushed every 2 s; beyond this gap the trail is stale
# enough that a stamp would mislead more than inform.
_MLAT_HISTORY_GT_MAX_DT_S = 90.0
# Live-ADS-B scoring reference for identified (seeded) solves whose hex has no
# synthetic trail: matches ADSB_SEED_MAX_DR_AGE_S in retina-analytics — beyond
# this a dead-reckoned fix is no longer a credible truth reference.
_MLAT_HISTORY_ADSB_MAX_DT_S = 45.0


_GT_NO_MATCH = {
    "gt_hex": None,
    "gt_error_km": None,
    "gt_lat": None,
    "gt_lon": None,
    "gt_speed_ms": None,
    "gt_heading_deg": None,
    "gt_source": None,
}


def _trail_velocity(trail, pt) -> tuple[float | None, float | None]:
    """Ground-truth speed/heading AT ``pt``, from the temporally nearest
    other point in the same trail.

    ``pt`` is excluded by identity, not value, so a duplicate-position point
    elsewhere in the trail is not mistaken for it.  |dt| > 0.1 s avoids
    dividing by a near-zero interval; the two points are then ordered by
    timestamp so the heading is the direction of travel, not its reverse.
    Returns (None, None) when the trail has no other usable point.
    """
    other = None
    best_dt = None
    for p in trail:
        if p is pt:
            continue
        dt = abs(p[3] - pt[3])
        if dt <= 0.1:
            continue
        if best_dt is None or dt < best_dt:
            best_dt, other = dt, p
    if other is None:
        return None, None
    earlier, later = (pt, other) if pt[3] <= other[3] else (other, pt)
    dt_s = later[3] - earlier[3]
    if dt_s <= 0:
        return None, None
    dist_km = _haversine_km(earlier[0], earlier[1], later[0], later[1])
    speed_ms = dist_km * 1000.0 / dt_s
    heading = bearing_deg(earlier[0], earlier[1], later[0], later[1])
    return speed_ms, heading


def _stamp_from_trail(gt_hex: str, trail, pt, err_km: float) -> dict:
    """GT stamp dict for a matched trail point, gt_source "trail".

    Shared by the proximity scan (_nearest_gt) and the hex-keyed bind in
    _gt_for_record — gt_speed_ms/gt_heading_deg are truth velocity AT ``pt``
    (see _trail_velocity), falling back to ground_truth_meta when the trail
    alone can't derive one (e.g. a single-point trail).  The proximity
    caller overwrites gt_source to "proximity" afterward.
    """
    gt_speed_ms, gt_heading_deg = _trail_velocity(trail, pt)
    if gt_speed_ms is None:
        meta = state.ground_truth_meta.get(gt_hex) or {}
        gt_speed_ms = meta.get("speed_ms")
        gt_heading_deg = meta.get("heading")
    return {
        "gt_hex": gt_hex,
        "gt_error_km": round(err_km, 3),
        "gt_lat": round(pt[0], 6),
        "gt_lon": round(pt[1], 6),
        "gt_speed_ms": round(gt_speed_ms, 1) if gt_speed_ms is not None else None,
        "gt_heading_deg": (round(gt_heading_deg, 1) if gt_heading_deg is not None else None),
        "gt_source": "trail",
    }


def _nearest_gt(lat: float, lon: float, ts_s: float) -> dict:
    """Nearest ground-truth trail point at solve time, no distance threshold.

    The dark-record (identity-less) path: a record with no adsb_hex scans
    every trail and accepts whichever point is closest.  Identified records
    go through _gt_for_record instead, which never proximity-binds — see
    its docstring for why.  Frozen into each history record so "how far was
    this solve really off, and from whom" survives later display dead-
    reckoning and GT re-binding.  Timestamp-closest point per trail — the
    same rule the MLAT verification matcher uses.
    """
    best_hex = best_km = best_pt = None
    best_trail = ()
    for gt_hex, trail in list(state.ground_truth_trails.items()):
        # tuple() snapshots the deque in one C call; iterating the live deque
        # here (min below, _trail_velocity after) raises "deque mutated during
        # iteration" when the sim-ingest thread appends a trail point — both
        # solver workers died exactly that way on staging (2026-08-08).
        trail = tuple(trail)
        if not trail:
            continue
        pt = min(trail, key=lambda p: abs(p[3] - ts_s))
        if abs(pt[3] - ts_s) > _MLAT_HISTORY_GT_MAX_DT_S:
            continue
        d = _haversine_km(lat, lon, pt[0], pt[1])
        if best_km is None or d < best_km:
            best_hex, best_km, best_pt, best_trail = gt_hex, d, pt, trail
    if best_hex is None:
        return dict(_GT_NO_MATCH)
    stamp = _stamp_from_trail(best_hex, best_trail, best_pt, best_km)
    stamp["gt_source"] = "proximity"
    return stamp


def _gt_for_record(adsb_hex, lat: float, lon: float, ts_s: float) -> dict:
    """Identity-aware GT stamp for one history record.

    A record carrying adsb_hex is scored against that identity ONLY — its
    GT trail if fresh, else its live dead-reckoned ADS-B fix, else abstain.
    Never the proximity scan: binding an identified solve to someone else's
    trail is how real aircraft got 200 km phantom errors (seeded solves of
    real hexes proximity-bound to whatever synthetic trail was nearest).
    Dark records (adsb_hex None) keep the legacy proximity scan.
    """
    hexn = normalize_hex_key(adsb_hex)
    if not hexn:
        return _nearest_gt(lat, lon, ts_s)
    # (a) own trail, same freshness rule as the proximity scan
    # tuple() snapshots the deque in one C call — see _nearest_gt above.
    trail = tuple(state.ground_truth_trails.get(hexn) or ())
    if trail:
        pt = min(trail, key=lambda p: abs(p[3] - ts_s))
        if abs(pt[3] - ts_s) <= _MLAT_HISTORY_GT_MAX_DT_S:
            return _stamp_from_trail(hexn, trail, pt, _haversine_km(lat, lon, pt[0], pt[1]))
    # (b) trail missing or stale — fall back to the live ADS-B fix,
    # dead-reckoned to solve time.  Deliberate: a stale synthetic trail with
    # a live sim ADS-B fix should still score, source "adsb".
    fix = state.adsb_aircraft.get(hexn) or state._adsb_for_seeding("real").get(hexn)
    if fix:
        f_lat, f_lon = fix.get("lat"), fix.get("lon")
        ts_fix_s = (fix.get("last_seen_ms") or 0) / 1000.0
        if (
            f_lat is not None
            and f_lon is not None
            and math.isfinite(f_lat)
            and math.isfinite(f_lon)
            and abs(ts_s - ts_fix_s) <= _MLAT_HISTORY_ADSB_MAX_DT_S
        ):
            gs_ms = (fix.get("gs", 0) or 0) * 0.514444
            trk = math.radians(fix.get("track", 0) or 0)
            ve, vn = gs_ms * math.sin(trk), gs_ms * math.cos(trk)
            dt = ts_s - ts_fix_s
            dr_lat, dr_lon = offset_latlon_m(f_lat, f_lon, ve * dt, vn * dt)
            return {
                "gt_hex": hexn,
                "gt_error_km": round(_haversine_km(lat, lon, dr_lat, dr_lon), 3),
                "gt_lat": round(dr_lat, 6),
                "gt_lon": round(dr_lon, 6),
                "gt_speed_ms": round(gs_ms, 1),
                "gt_heading_deg": round(float(fix.get("track", 0) or 0), 1),
                "gt_source": "adsb",
            }
    # (c) abstain — an identified solve is never scored against another
    # identity, even a nearby one.
    return dict(_GT_NO_MATCH)


def _stamp_foreign_nodes(rec: dict) -> None:
    """Stamp which of a dark record's own nodes could not see the aircraft.

    Cluster contamination is the dark lane's largest known defect — a
    candidate assembled by format_track_pairs_for_solver can carry a node
    whose track belongs to a *different* aircraft, and the solver then fits a
    geometry no single aircraft ever occupied.  Offline the audit measured it
    at ~60 % of dark candidates; this makes the same number live.

    The test is the associator's own visibility predicate applied whole
    (retina_analytics.association._point_in_beam against the registered
    NodeGeometry), which is the same gate known-lane claiming uses — claiming
    and the dark lane must mean the same thing by "this node can see there",
    and a second bespoke rule here would let the two disagree.  Two
    consequences worth knowing: it is a ground-projected bearing/footprint
    test with no altitude term, and under FOV_MODE=active it is the learned
    FOV rather than the theoretical wedge.  Both are exactly what the rest of
    the pipeline believes about coverage, which is the point.

    Position is the matched ground-truth point already stamped on the record
    (gt_lat/gt_lon at the solve epoch), so this costs no extra trail lookup —
    only one cone test per contributing node.  Nodes trimmed out by
    _trim_and_resolve are included: a node dropped for a bad residual is
    precisely the contamination this measures, and leaving it out would hide
    every case trimming already rescued.

    A node with no registered geometry is not judged either way.  When that
    leaves nothing judgeable the record is left unstamped rather than stamped
    clean, so contamination_pct never counts an abstention as innocence.
    """
    lat, lon = rec.get("gt_lat"), rec.get("gt_lon")
    if lat is None or lon is None:
        return
    node_ids = list(rec.get("contributing_node_ids") or [])
    node_ids += [nid for nid in (rec.get("trimmed_node_ids") or []) if nid not in node_ids]
    if not node_ids:
        return
    geometries = state.node_associator.node_geometries
    judged = 0
    foreign: list[str] = []
    for nid in node_ids:
        geo = geometries.get(nid)
        if geo is None:
            continue
        judged += 1
        if not _point_in_beam(lat, lon, geo):
            foreign.append(nid)
    if not judged:
        return
    rec["foreign_node_ids"] = foreign
    rec["contaminated"] = bool(foreign)


def _record_dark_accuracy_sample(rec: dict) -> None:
    """Offer one published DARK solve to the rolling accuracy store.

    state.accuracy_samples had no dark writer at all: the only general one is
    track_gates._record_accuracy_sample, which sits behind ``if adsb_lat and
    adsb_lon`` on the ADS-B enrichment path, so a solve with no transponder
    identity could never produce a sample no matter how accurate it was.
    health.py's solver_accuracy_degraded is computed from those samples, which
    meant the alert claimed to cover "multi-node" accuracy while structurally
    knowing nothing about the multinode lane that has no ADS-B.

    position_source stays ``multinode_solve`` — not a new source name —
    because that is genuinely what these tracks are published as
    (aircraft_feed.py stamps multinode_solve on dark and tagged multinode
    tracks alike), so this closes a sampling hole rather than inventing a
    category; ``lane`` carries the split for anyone who needs it back.  The
    error is gt_error_km, distance to simulation ground truth, which for a
    dark solve is the only truth there is; where there are no ground-truth
    trails (production) nothing is stamped and this samples nothing, so the
    alert's inputs there are byte-identical to before.

    Unthrottled, unlike the known lane's sampler: dark publishes run ~0.13/s
    on the test fleet against that lane's ~8/s, three orders off the rate that
    made throttling necessary to stop one source evicting the 5 000-sample
    store.
    """
    state.accuracy_samples.append(
        {
            "hex": rec.get("gt_hex") or rec.get("solver_hex"),
            "error_km": round(float(rec["gt_error_km"]), 4),
            "position_source": "multinode_solve",
            "lane": "dark",
            "n_nodes": rec.get("n_nodes") or 0,
            "ts": round(rec["ts_ms"] / 1000.0, 1),
        }
    )


def _record_solve_history(
    outcome: str,
    s_in,
    result: dict | None,
    *,
    solve_key: str | None = None,
    raw_lat: float | None = None,
    raw_lon: float | None = None,
    displacement_km: float | None = None,
    chi2_per_dof: float | None = None,
    key_how: str | None = None,
    key_dist_km: float | None = None,
    key_dt_s: float | None = None,
    follow_key: str | None = None,
    superseded_keys: list[str] | None = None,
    superseded_blocked: int | None = None,
    extra: dict | None = None,
) -> None:
    """Append one solve outcome to state.mlat_solve_history.

    ``raw_lat/raw_lon`` is the solver's own position before EWMA smoothing;
    for published records ``lat/lon`` additionally carries the smoothed
    position actually stored in multinode_tracks.  Rejected solves have no
    track key (it is minted after the gates), so ``solver_hex`` is None for
    them and lookup by map ID returns published records plus nearby rejects.

    ``key_how``/``key_dist_km``/``key_dt_s`` are multinode_key_decision's
    verdict for this solve — which branch produced solve_key, how far the
    solve landed from the entry it was keyed onto, and the signed measurement
    age of that entry at this solve's epoch (negative = the entry was measured
    after this solve; see _MN_ASSOC_MAX_NEG_DT_S).  Only the publish path has
    run the keying rule, so all three are None on every reject (the key is
    minted after the gates, which is also why solver_hex is None there) — with
    one exception: a ``shadowed_by_follow`` reject IS the keying rule's
    verdict, and carries key_how/key_dist_km/key_dt_s plus ``follow_key``
    naming the followed key it was refused in favour of.

    ``follow_key`` overrides the input's own follow_key for the record only.
    The ghost guard below is deliberately NOT fed from it: the guard judges
    solves the FOLLOW lane produced, and a shadowed record is a bottom-up
    solve that merely names a followed key — feeding it there would let the
    bottom-up lane's refusals drop the very track that refused them.

    ``superseded_keys``/``superseded_blocked`` are the other side of that
    decision: which existing entries this publish popped as the same aircraft
    (see _supersession_match), and how many entries shared a source track id
    with it but were refused.  Both are publish-path only, for the same reason
    key_how is — nothing before the gates has run supersession.

    ``extra`` merges caller-supplied fields (trim metadata, beam-rejection
    diagnostics) into the record.  Applied before the GT stamp so it can
    never clobber gt_hex/gt_error_km/gt_lat/gt_lon — and so the trimmed node
    ids it carries are in hand for the contamination stamp below.

    ``foreign_node_ids``/``contaminated`` are stamped on DARK records that
    matched ground truth: which of this candidate's own nodes could not see
    the aircraft it was matched to (see _stamp_foreign_nodes).  Absent on
    every other record, which is what /api/test/solver-stats' contamination
    block counts as "not judged" rather than as clean.
    """
    r = result if isinstance(result, dict) else {}
    s = s_in if isinstance(s_in, dict) else {}
    now_ms = int(time.time() * 1000)
    if raw_lat is None:
        raw_lat = r.get("lat")
        raw_lon = r.get("lon")
    ig = s.get("initial_guess") or {}
    if displacement_km is None and raw_lat is not None and ig.get("lat") and ig.get("lon"):
        displacement_km = _haversine_km(float(ig["lat"]), float(ig["lon"]), float(raw_lat), float(raw_lon))
    _dark = solve_key.startswith("mn-dark-") if solve_key else displacement_caps._is_dark_solver_input(s)
    _sigma_m = solve_sigma_m(r, dark=_dark)
    rec = {
        "ts_ms": now_ms,
        "measurement_ts_ms": int(r.get("timestamp_ms") or s.get("timestamp_ms") or 0),
        "outcome": outcome,
        "solve_key": solve_key,
        "solver_hex": multinode_hex_from_key(solve_key) if solve_key else None,
        "n_nodes": int(r.get("n_nodes") or s.get("n_nodes") or 0),
        "contributing_node_ids": list(r.get("contributing_node_ids") or []),
        # How many nodes the association round HAD for this aircraft, against
        # which n_nodes above is the number it actually solved with (see
        # InterNodeAssociator._shared_track_pools: the node set of the
        # shared-track component this input was clustered out of).  A published
        # 2-node solve whose pool is 3 is a node the round paired and the
        # position clustering then left in a separate input — which is the only
        # way to tell that case apart from the third node never pairing at all.
        # None on inputs that never went through that clustering (anchored /
        # known-lane, dark-follow predictions).
        "pool_n_nodes": s.get("pool_n_nodes"),
        # Association's side of an n=2 pairing, on EVERY record rather than
        # just published ones.  n_epochs and cv_epochs_present are the two
        # halves of the n2_unconfirmed story: 142 of 165 rejects in a droplet
        # capture had no cv_epochs at all (both node tracks must span
        # N2_CONFIRM_MIN_SPAN_S before association attaches them), so "no fit"
        # and "bad fit" are distinguishable from the dump alone.  track_ids is
        # the input's own tracklet ids — a reject carries an empty
        # source_track_ids because that is rebuilt post-trim on the publish
        # path only, which made it impossible to follow one rejected pairing
        # across rounds and see whether it ever earned its way through.
        "n_epochs": s.get("n_epochs"),
        "cv_epochs_present": bool(s.get("cv_epochs")),
        "track_ids": list(s.get("track_ids") or []),
        "adsb_hex": s.get("adsb_hex"),
        # Set only on an anchored solver input (top-down claiming, active
        # mode) — present on rejects too, not just "published", so the
        # windowed fragmentation breakdown can see what fraction of ALL
        # attempts (not just successful ones) were anchor-carrying.
        "anchor_key": s.get("anchor_key"),
        # Lane provenance, carried by the solver input rather than inferred
        # from the key: a dark-follow solve (services/dark_follow.py) lands on
        # an mn-dark-* key and would otherwise be indistinguishable from the
        # bottom-up solves whose funnel it is not part of.  guess_source says
        # what the initial guess WAS — "prediction" for a followed track,
        # absent for the association grid centroid every other dark input
        # carries — and follow_key names the track that predicted it.
        "lane": s.get("lane"),
        "guess_source": s.get("guess_source"),
        "follow_key": follow_key or s.get("follow_key"),
        "raw_lat": round(float(raw_lat), 6) if raw_lat is not None else None,
        "raw_lon": round(float(raw_lon), 6) if raw_lon is not None else None,
        "lat": round(float(r["lat"]), 6) if outcome == "published" else None,
        "lon": round(float(r["lon"]), 6) if outcome == "published" else None,
        "alt_m": round(float(r.get("alt_m") or 0), 0),
        "vel_east": round(float(r.get("vel_east") or 0), 1),
        "vel_north": round(float(r.get("vel_north") or 0), 1),
        "rms_delay": round(float(r.get("rms_delay") or 0), 3),
        "rms_doppler": round(float(r.get("rms_doppler") or 0), 2),
        "chi2_per_dof": round(float(chi2_per_dof), 3) if chi2_per_dof is not None else None,
        # Solve-quality observability for the KF smoother in
        # services/track_filter.py: pos_sigma_km is the solver's own
        # cov_en_km2-derived sigma (pre-inflation), kf_pos_sigma_m is the
        # filter's post-update marginal — both absent on solves the smoother
        # never touched (rejects, off/ewma mode, first-ever solve for a key).
        "pos_sigma_km": round(float(r["pos_sigma_km"]), 3) if r.get("pos_sigma_km") is not None else None,
        "kf_pos_sigma_m": r.get("kf_pos_sigma_m"),
        # Which branch of the display filter produced this position, and the
        # two numbers it decided from: kf_d2 is the innovation's chi² against
        # the filter's own covariance, kf_innov_m the innovation magnitude in
        # metres.  Present on every result the KF touched (see
        # track_filter._stamp), None on rejects and in off/ewma mode.  They
        # let a capture say how a key's bad joins arrived — "reanchored" vs
        # "manoeuvre_rescued" vs "smoothed" — without flipping any policy.
        "kf_action": r.get("kf_action"),
        "kf_d2": r.get("kf_d2"),
        "kf_innov_m": r.get("kf_innov_m"),
        # The calibrated display sigma (services/solve_uncertainty.py), stamped
        # here so the calibration that produced it can be re-run from
        # /api/test/mlat-history alone: fraction(gt_error_km*1000 <=
        # 2.448*sigma_m) over published records should stay near 0.95.  Lane
        # comes from the track key when there is one (the same authority
        # multinode_to_aircraft uses); rejects have no key yet, so their lane
        # falls back to whether the solver input carried an ADS-B identity.
        "sigma_m": (round(_sigma_m, 1) if _sigma_m is not None else None),
        "guess_lat": round(float(ig["lat"]), 6) if ig.get("lat") else None,
        "guess_lon": round(float(ig["lon"]), 6) if ig.get("lon") else None,
        "guess_alt_km": ig.get("alt_km"),
        # Where that guess altitude came from: "key" (inherited from an
        # established 3+-node dark entry, _inherit_key_altitude), "anchor"
        # (the follow lane's own prediction) or "grid" (the association
        # weighted mean).  Stamped so the live effect is measurable directly
        # off /api/test/mlat-history — gt_error split by alt_source at n=2 is
        # the number this inheritance exists to move.
        "alt_source": s.get("alt_source") if isinstance(s, dict) else None,
        "displacement_km": round(displacement_km, 3) if displacement_km is not None else None,
        # Which displacement cap judged this solve.  Stamped on every record,
        # not only rejected_displacement, so /api/test/mlat-history can read a
        # published solve's displacement against the cap that let it through
        # and a reject's against the cap that killed it.
        "displacement_cap_km": displacement_caps.displacement_cap_km(s, r, dark=_dark),
        # How this solve got its key, and how far it was from the entry it
        # was keyed onto (see multinode_key_decision).  Fragmentation is a
        # question about key DECISIONS, and until now the history recorded
        # only the key that came out: a minted key and a re-key onto an
        # existing one were indistinguishable after the fact, so the
        # age-scaled proximity gate could not be measured against the flat
        # one it replaced.  key_dist_km is the dead-reckoned distance for a
        # proximity match, the flat anchor distance for an anchor hit, and
        # None where nothing was matched (adsb, minted) or the record never
        # reached keying at all (every reject).
        "key_how": key_how,
        "key_dist_km": round(float(key_dist_km), 3) if key_dist_km is not None else None,
        # ...and how far apart in MEASUREMENT time the two were, signed.
        # Negative means this solve's own epoch is the older one — the
        # out-of-order arrival the signed dt window admits — and separates a
        # re-key the old rule would also have made from one it refused.
        "key_dt_s": round(float(key_dt_s), 1) if key_dt_s is not None else None,
        # Supersession's verdict for this publish (see _supersession_match):
        # the entries it popped as this same aircraft, and the count of
        # entries that shared a source track id but were refused.  Empty/0 on
        # every reject — supersession runs only on the publish path.
        "superseded_keys": list(superseded_keys or []),
        "superseded_blocked": int(superseded_blocked or 0),
        "solve_count": r.get("solve_count"),
        "source_track_ids": list(r.get("source_track_ids") or []),
        "vel_source": r.get("vel_source"),
        "vz_saturated": bool(r.get("vz_saturated")),
        "vel_untrusted": bool(r.get("vel_untrusted")),
        "solver_vel_east": (round(float(r["solver_vel_east"]), 1) if r.get("solver_vel_east") is not None else None),
        "solver_vel_north": (round(float(r["solver_vel_north"]), 1) if r.get("solver_vel_north") is not None else None),
        # Which position was published, and what the other candidate said.
        # Only a confirmed n=2 solve has two (see _apply_n2_fit_position): the
        # constant-velocity fit over the pairing's whole epoch history, and the
        # single-epoch LM solve kept here as solve_raw_lat/solve_raw_lon.
        # pos_source is None on every other record — nothing else chooses — so
        # gt_error by pos_source over published n=2 records measures the swap
        # live, and fit_vs_solve_km says how far apart the two answers were on
        # the solves where it made no difference to the score.
        "pos_source": r.get("pos_source"),
        "solve_raw_lat": (round(float(r["solve_raw_lat"]), 6) if r.get("solve_raw_lat") is not None else None),
        "solve_raw_lon": (round(float(r["solve_raw_lon"]), 6) if r.get("solve_raw_lon") is not None else None),
        "fit_vs_solve_km": (round(float(r["fit_vs_solve_km"]), 3) if r.get("fit_vs_solve_km") is not None else None),
        # Whether the published fit position came from a pinned-altitude fit.
        # None on every record that did not swap a position in; the point of
        # carrying it is that alt_error by fit_altitude_fixed is the live read
        # on the pin, the same way gt_error by pos_source is on the swap.
        "fit_altitude_fixed": (bool(r["fit_altitude_fixed"]) if r.get("fit_altitude_fixed") is not None else None),
    }
    if extra:
        rec.update(extra)
    # The INPUT's follow_key, not the record's: a shadowed_by_follow reject
    # names a followed key it was refused in favour of, and that key's guard
    # must not hear about a solve the follow lane never made.
    _follow_key = s.get("follow_key")
    if _follow_key:
        # The dark-follow ghost guard (services/dark_follow.py) needs a verdict
        # for every follow-solve, and this is the one place all of them pass
        # through — published, every rejected_* gate, unconverged, and the
        # shadow pass's own record.  Following a track is a feedback loop (the
        # solve keeps the key alive, the key keeps claiming detections), so a
        # key that stops earning its solves has to be droppable from OUTSIDE
        # that loop.  A shadow record carries its own verdict in follow_ok:
        # it never reached the gates, so "did it publish" says nothing.
        #
        # ...with one exception.  An n2_unconfirmed record means the solve was
        # WITHHELD for lack of a third node, not refuted: with the anchored n=2
        # bypass off (DARK_FOLLOW_N2_ADMIT, default), a follow input built from
        # exactly two claiming nodes carries no cv_epochs and so cannot clear
        # the confirmation gate however right the prediction was.  Feeding that
        # to the guard as a reject cost the key its target status after two of
        # them — measured on the test droplet, 43-56 drops per 20-minute
        # capture and 1146-1409 key-seconds of `cooldown`, the lane's biggest
        # ineligibility bucket by a wide margin, during which the bottom-up
        # lane had to re-find the aircraft from scratch at the 5-8% publish
        # rate its own n=2 candidates manage.  So the guard hears nothing at
        # all here: not ok (the prediction earned no confirmation either) and
        # not reject.  The key still ends on DARK_FOLLOW_MAX_AGE_S when no
        # wider claim arrives, which is the honest end of following.
        if outcome == "n2_unconfirmed":
            state.bump_counter("dark_follow_n2_withheld")
        else:
            dark_follow.record_outcome(_follow_key, bool(rec.get("follow_ok", outcome == "published")))
        if outcome == "published":
            state.bump_counter("dark_follow_published")
            # ...and the lane now owns the key it published on, for
            # DARK_FOLLOW_OWN_S.  Stamped with the MEASUREMENT epoch, because
            # the reader (multinode_key_decision) compares it against another
            # solve's measurement epoch and is deliberately clock-free.  The
            # key published on, not the anchor: on the rare anchor fallback
            # the lane's solve went somewhere else, and that is the entry it
            # is now refreshing.
            dark_follow.note_follow_publish(solve_key or _follow_key, rec["measurement_ts_ms"] / 1000.0)
    if raw_lat is not None and raw_lon is not None:
        meas_ts_s = (rec["measurement_ts_ms"] or now_ms) / 1000.0
        rec.update(_gt_for_record(rec["adsb_hex"], float(raw_lat), float(raw_lon), meas_ts_s))
    else:
        rec.update(_GT_NO_MATCH)
    # Direction error: how far off the solved velocity vector points from
    # ground truth at the matched trail point.  Heading is undefined near
    # hover, so this (and the vector-norm error below) only assert anything
    # once truth is actually moving and the solve has a direction to compare.
    gt_speed_ms = rec.get("gt_speed_ms")
    gt_heading_deg = rec.get("gt_heading_deg")
    ve, vn = rec["vel_east"], rec["vel_north"]
    if gt_heading_deg is not None and gt_speed_ms and gt_speed_ms >= 20.0 and math.hypot(ve, vn) > 1.0:
        solved_heading = math.degrees(math.atan2(ve, vn)) % 360
        rec["heading_err_deg"] = round(abs((solved_heading - gt_heading_deg + 180.0) % 360.0 - 180.0), 1)
    else:
        rec["heading_err_deg"] = None
    if gt_speed_ms is not None and gt_heading_deg is not None:
        gt_ve = gt_speed_ms * math.sin(math.radians(gt_heading_deg))
        gt_vn = gt_speed_ms * math.cos(math.radians(gt_heading_deg))
        rec["vel_err_ms"] = round(math.hypot(ve - gt_ve, vn - gt_vn), 1)
    else:
        rec["vel_err_ms"] = None
    # Live cluster-contamination metric, dark lane only and only where ground
    # truth actually matched — without a truth position there is nothing to
    # ask "could this node see it?" about.  See _stamp_foreign_nodes.
    if _dark and rec.get("gt_hex"):
        _stamp_foreign_nodes(rec)
    if rec["outcome"] == "published" and _dark and rec.get("gt_error_km") is not None:
        _record_dark_accuracy_sample(rec)
    # Route by lane: the known lane's per-hex-per-pass volume would otherwise
    # evict dark records from the shared cap long before they aged out (see
    # state.mlat_solve_history_known).  Readers merge the two.
    buf = state.mlat_solve_history_known if rec.get("known_lane") else state.mlat_solve_history
    buf.append(rec)
    # Age-prune from the left; append/popleft are atomic, and a racing second
    # writer at worst re-checks an already-fresh head.
    try:
        while buf and now_ms - buf[0]["ts_ms"] > _MLAT_HISTORY_MAX_AGE_MS:
            buf.popleft()
    except IndexError:
        pass
