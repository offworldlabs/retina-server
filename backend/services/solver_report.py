"""Solve-history readers and the Solver Report computation.

Pure computation over the solve-history deques in ``core.state``: lane
classification, per-lane capping, publication shaping, and the windowed
funnel/error/ghost/consensus picture that ``/api/test/solver-stats`` and
``/api/test/mlat-history`` serve.  No routing lives here.
"""

import time

from core import state
from services import dark_follow, track_filter
from services.geo import haversine_km
from services.id_utils import is_transponder_hex
from services.node_refs import public_records
from services.public_geometry import without_receiver_geometry
from services.tasks import solver as solver_mod

# ── Solve-history readers (both lanes) ───────────────────────────────────────
# The solver keeps one deque per lane — state.mlat_solve_history for the
# regular pipeline, state.mlat_solve_history_known for the known lane — so the
# known lane's per-hex-per-pass volume can no longer evict dark records from a
# shared cap long before they age out.  Every reader below merges the two, so
# the split is a retention fix and not a visibility regression: exactly the
# same records are queryable as before.


def _merged_solve_history() -> list[dict]:
    """Both lanes' solve records, oldest first.

    ``list(deque)`` per side is the established unlocked snapshot idiom (see
    core/state.py's provider helpers): a racing solver-thread append is at
    worst missed by one record, never seen half-written.
    """
    records = list(state.mlat_solve_history)
    records.extend(state.mlat_solve_history_known)
    records.sort(key=lambda r: r["ts_ms"])
    return records


def _window_effective_minutes(records: list[dict], minutes: float) -> float:
    """How much of the requested window the stores actually hold, in minutes.

    Age of the oldest record present, capped at the request.  A value below
    ``minutes`` means the answer IS truncated — the process has not been up
    that long, or a deque hit its maxlen — and that has to be legible: the
    shared-deque era answered ``?minutes=35`` out of ~18 min of records with
    nothing in the payload to say so, which reads as a quiet period rather
    than a short store.  ``records`` must be sorted oldest-first.
    """
    if not records:
        return 0.0
    age_s = max(0.0, time.time() - records[0]["ts_ms"] / 1000.0)
    return round(min(minutes, age_s / 60.0), 2)


def _record_lane(rec: dict) -> str:
    """Which solver lane produced one history record: "known", "dark_follow",
    "dark" or "adsb".

    ``known_lane`` is stamped by known_lane._attempt via ``extra``.  For the
    regular pipeline the authority is the minted track key (mn-dark-* vs
    mn-adsb-*, multinode_identity.multinode_key_decision); a reject is recorded
    before any key exists, so it falls back to the same predicate that key
    decision uses — whether the solver input carried a transponder-shaped
    identity, which is also what picked its displacement cap.

    ``lane`` is checked before the key, because a dark-follow record
    (services/dark_follow.py) is keyed mn-dark-* by design — it is the same
    aircraft the dark lane tracks, reached top-down — and would otherwise land
    in the bottom-up funnel whose attempts and rejects it is not one of.
    """
    if rec.get("known_lane"):
        return "known"
    if rec.get("lane") == "dark_follow":
        return "dark_follow"
    key = rec.get("solve_key")
    if key:
        return "dark" if key.startswith("mn-dark-") else "adsb"
    hexn = rec.get("adsb_hex")
    return "adsb" if hexn and is_transponder_hex(hexn) else "dark"


_LANES = ("dark", "known", "adsb", "dark_follow")


def _world_funnels(records: list[dict]) -> dict:
    """Windowed denominators, so a synthetic fleet cannot mask real failures.

    Known/ADS-B lanes are explicitly assisted. Dark-lane inputs can still have
    learned coverage/bias from ADS-B: a fully blind benchmark is the isolated
    replay tool, not this production publication funnel.
    """
    worlds = {}
    for rec in records:
        node_ids = rec.get("contributing_node_ids") or []
        labels = {state.node_world(nid) for nid in node_ids}
        world = rec.get("world") or (next(iter(labels)) if len(labels) == 1 else "mixed" if labels else "unknown")
        lanes = worlds.setdefault(world, {})
        lane = lanes.setdefault(_record_lane(rec), {"attempts": 0, "published": 0, "truth_scored": 0})
        lane["attempts"] += 1
        lane["published"] += bool(rec.get("published") or rec.get("outcome") == "published")
        lane["truth_scored"] += rec.get("gt_error_km") is not None
    for lanes in worlds.values():
        for lane in lanes.values():
            lane["publish_rate"] = lane["published"] / lane["attempts"]
    return worlds


def _cap_per_lane(records: list[dict], limit: int) -> list[dict]:
    """Keep the ``limit`` newest records OF EACH LANE, newest first.

    ``records`` must already be newest-first.  A single flat ``[:limit]``
    made the cap a race between lanes rather than a retention rule, exactly
    as the shared deque did before PR #289 split it: the known lane writes
    ~16x the dark lane's volume, so a flat 1 000-record answer to a 30 min
    request held only the newest ~6 min of dark records and the rest of the
    window read as a quiet period.  Capping per lane means known-lane volume
    can never evict a dark record from a response.
    """
    kept: list[dict] = []
    counts: dict[str, int] = {}
    for r in records:
        lane = _record_lane(r)
        n = counts.get(lane, 0)
        if n >= limit:
            continue
        counts[lane] = n + 1
        kept.append(r)
    return kept


def _published_records(records) -> list[dict]:
    """Solve records as the unauthenticated /api/test/mlat-history route
    serves them.

    Withhold the geometry, then republish the identities, in that order: the
    withheld fields sit beside the node id they were measured for, and one of
    them (`foreign_node_ids`) is an identity field as well as a measurement, so
    it has to go before the identity pass renames it.

    Both passes are structural (services/public_geometry.py for the geometry,
    services/node_refs.public_records for the identities) because these
    records are written by a dozen solver call sites under whatever keys each
    one chose.  One missed identity key would publish a raw id beside the
    adsb_hex the aircraft feed carries against contributing_node_refs, which
    recovers the mapping for the whole fleet from two anonymous requests.
    """
    return public_records(without_receiver_geometry(r) for r in records)


# ── Solver Report (full funnel/error/ghost/consensus picture) ─────────────────

# Both tunable from staging observation, not derived from anything physical.
_GHOST_GATE_KM = 5.0
_ERR_GT_GATE_KM = 15.0
# How stale a ground-truth trail point (or ADS-B fix) may be and still count
# as "the aircraft was there" for ghost detection.
_GHOST_GT_MAX_AGE_S = 90.0
_ADSB_FRESH_S = 60.0


def _n_nodes_bucket(n_nodes) -> str:
    """The by_n_nodes bucket one record falls in.

    2/3/4 are their own buckets because that is where the interesting cliff
    sits — an n=2 solve is a bistatic intersection, n=3 is barely
    overdetermined, n>=4 is where the dark lane behaves.  Everything from 5 up
    is one bucket ("5+") for the same reason the funnel above stops at n3plus:
    the sample thins out fast and the differences stop being informative.
    "<2" catches records that carry no usable node count (0, 1 or a missing
    field, which some early reject paths write) so the buckets still sum to
    attempts rather than quietly losing rows.
    """
    n = int(n_nodes or 0)
    if n < 2:
        return "<2"
    if n >= 5:
        return "5+"
    return str(n)


def _windowed_ghosts(published_records: list[dict]) -> dict:
    """Ghost count over one window's published DARK records.

    judged     — records with a gt_error_km stamp whose contributing nodes are
                 all simulated (state.node_world == "sim").  A real node's
                 solve has no trail to be judged against: solver._gt_nearest
                 has no distance cap, so it would carry the distance to the
                 nearest SIMULATED trail — hundreds of km — and read as a
                 ghost.  A record with no node ids cannot be placed in a world
                 and is judged on its stamp alone.
    ghosts     — judged records with gt_error_km > _GHOST_GATE_KM.
    one_shot   — of those, records whose solve_count is 1: the n>=3 preview
                 that aircraft_feed withdraws after MN_ONESHOT_TTL_S, which is
                 the population a live snapshot almost never catches.
    by_n_nodes — judged/ghosts/ghost_pct per _n_nodes_bucket, because the
                 ghost rate is a function of n (n=3 is where it lives) and
                 the total alone hides that.
    """
    judged = 0
    ghosts = 0
    one_shot = 0
    unjudged_real = 0
    buckets: dict[str, list[int]] = {}
    for r in published_records:
        err = r.get("gt_error_km")
        if err is None:
            continue
        if any(state.node_world(nid) != "sim" for nid in (r.get("contributing_node_ids") or [])):
            unjudged_real += 1
            continue
        judged += 1
        b = buckets.setdefault(_n_nodes_bucket(r.get("n_nodes")), [0, 0])
        b[0] += 1
        if err > _GHOST_GATE_KM:
            ghosts += 1
            b[1] += 1
            if r.get("solve_count") == 1:
                one_shot += 1
    order = {"<2": 0, "2": 1, "3": 2, "4": 3, "5+": 4}
    return {
        "published": len(published_records),
        "judged": judged,
        "unjudged_real_world": unjudged_real,
        "ghosts": ghosts,
        "one_shot_ghosts": one_shot,
        "precision_pct": round((judged - ghosts) / judged * 100, 1) if judged else None,
        "by_n_nodes": {
            label: {
                "judged": b[0],
                "ghosts": b[1],
                "ghost_pct": round(b[1] / b[0] * 100, 1) if b[0] else None,
            }
            for label, b in sorted(buckets.items(), key=lambda kv: order.get(kv[0], 99))
        },
    }


def _by_n_nodes(records: list[dict]) -> dict:
    """Per-attempt outcomes bucketed by how many nodes went into the solve.

    The question this answers is "what fraction of n-node candidates actually
    reach the map, and what stops the rest?" — until it existed that needed an
    offline pass over a history dump, which is how the 3-node dark starvation
    was found in the first place.  Windowed over the same records the funnel
    above uses, and the reject-reason stripping is deliberately identical to
    the by_reason code there so the two tables can be added up.

    gt_err_median_km carries the same <= _ERR_GT_GATE_KM gate as the top-level
    position_error_km, so the per-bucket medians and the overall one are the
    same population split up rather than two different ones.
    """
    buckets: dict[str, dict] = {}
    for r in records:
        b = buckets.setdefault(
            _n_nodes_bucket(r.get("n_nodes")), {"attempts": 0, "published": 0, "rejects": {}, "errs": []}
        )
        b["attempts"] += 1
        outcome = r.get("outcome")
        if outcome == "published":
            b["published"] += 1
            err = r.get("gt_error_km")
            if err is not None and err <= _ERR_GT_GATE_KM:
                b["errs"].append(err)
        else:
            reason = outcome[len("rejected_") :] if outcome.startswith("rejected_") else outcome
            b["rejects"][reason] = b["rejects"].get(reason, 0) + 1

    out: dict[str, dict] = {}
    for label, b in buckets.items():
        errs = sorted(b["errs"])
        top = sorted(b["rejects"].items(), key=lambda kv: (-kv[1], kv[0]))[:4]
        out[label] = {
            "attempts": b["attempts"],
            "published": b["published"],
            "publish_rate": round(b["published"] / b["attempts"], 3) if b["attempts"] else None,
            # Top four reasons only: the tail is long (every gate in the solver
            # has a label) and the whole distribution is already in by_reason.
            "rejects": dict(top),
            "gt_err_median_km": errs[len(errs) // 2] if errs else None,
        }
    # Sorted so the JSON reads 2, 3, 4, 5+ rather than in dict-insertion order,
    # which is whatever order the window happened to arrive in.
    order = {"<2": 0, "2": 1, "3": 2, "4": 3, "5+": 4}
    return {k: out[k] for k in sorted(out, key=lambda k: order.get(k, 9))}


def _solver_window_stats(minutes: float) -> dict:
    """Full solver picture: publication funnel, position error, ghost/false-track
    precision, consensus counters — the data behind the Solver Report panel.

    THE TOP-LEVEL FUNNEL IS THE DARK LANE ONLY.  ``attempts``, ``published``,
    ``rejects``, ``position_error_km`` and ``fragmentation`` describe the
    multinode lane that has no transponder to lean on, which is the lane those
    keys were written to measure and the only one whose numbers a publication
    funnel makes sense of.  Every record in the window is classified by
    ``_record_lane`` and the denominators are published as ``lane_split``.

    They were not always: with all three lanes in one deque, "attempts" was
    every record and "a reject" was every non-``published`` outcome, so the
    known lane's own SUCCESS label (``known_truth_match``) was reported as a
    reject reason.  Live, that read 4 475 attempts / 80 published with
    ``known_truth_match: 3296`` heading the reject table — 94 % of the funnel
    belonged to a lane that does not publish through it, and the dark lane's
    real 265 → 80 was unrecoverable from the payload.  The known lane's own
    numbers live in the ``known_lane`` block; the ADS-B-anchored lane is
    counted in ``lane_split`` (it has its own accuracy endpoints and does not
    need a second funnel).

    Funnel, reject-reason and position-error stats are windowed over the last
    ``minutes`` of both solve-history deques merged (idiom shared with
    mlat-history), with ``window_effective_minutes`` reporting how much of
    that window is actually held.  Consensus/counters read *current* live
    state rather than the window.

    GHOSTS ARE WINDOWED TOO (``ghosts``), over the same published dark
    records as the funnel: a ghost is a published dark solve whose stamped
    gt_error_km (solver._gt_nearest — distance to the time-nearest point of
    the nearest ground-truth trail at the solve epoch) exceeds _GHOST_GATE_KM.
    Records with no GT stamp, and records with a contributing node from the
    real world (state.node_world) — where no trail exists to judge against —
    are "not judged" and stay out of the denominator, so precision_pct is
    None rather than 100 where nothing could be scored.

    It used to be a point-in-time scan of state.multinode_tracks instead,
    and that scan is still published as ``ghosts.live`` — but it could not
    see the ghosts that matter.  The dark lane's ghosts are overwhelmingly
    short-lived: an n=3 one-shot is withdrawn after MN_ONESHOT_TTL_S (15 s)
    and a wrong mint is superseded or expires within a minute, so a snapshot
    taken at request time reported 0 ghosts / 100 % precision on a test
    droplet whose solve history held 58 published dark solves more than 3 km
    from any truth in the same 35 minutes.  The window sees every one of
    them.  The live scan's ADS-B rescue (``adsb_near``) is not needed by the
    windowed number: a sim node's solves are judged against the simulated
    world, which now mirrors the live traffic it echoes, and real-node solves
    are not judged at all.
    """
    cutoff_ms = int((time.time() - minutes * 60.0) * 1000)
    merged = _merged_solve_history()
    effective_minutes = _window_effective_minutes(merged, minutes)
    all_records = [r for r in merged if r["ts_ms"] >= cutoff_ms]

    lane_split = {"dark": 0, "adsb": 0, "known": 0, "dark_follow": 0}
    records: list[dict] = []
    known_records: list[dict] = []
    for r in all_records:
        lane = _record_lane(r)
        lane_split[lane] = lane_split.get(lane, 0) + 1
        if lane == "dark":
            records.append(r)
        elif lane == "known":
            known_records.append(r)

    attempts = len(records)
    n2 = n3plus = 0
    reject_total = 0
    by_reason: dict[str, int] = {}
    pos_errors: list[float] = []
    for r in records:
        outcome = r.get("outcome")
        if outcome == "published":
            n_nodes = r.get("n_nodes") or 0
            if n_nodes == 2:
                n2 += 1
            elif n_nodes >= 3:
                n3plus += 1
            err = r.get("gt_error_km")
            if err is not None and err <= _ERR_GT_GATE_KM:
                pos_errors.append(err)
        else:
            reject_total += 1
            reason = outcome[len("rejected_") :] if outcome.startswith("rejected_") else outcome
            by_reason[reason] = by_reason.get(reason, 0) + 1

    # ── cluster contamination (dark, windowed) ──────────────────────────────
    # Of the dark records this window that matched ground truth, how many
    # carried a node that could not see the aircraft they were matched to —
    # the live version of the offline number Phase 2 exists to move (~60 %).
    # Records without the stamp are records nothing could be asked about (no
    # GT match, or no registered geometry for any contributing node) and stay
    # out of the denominator rather than counting as clean; see
    # solve_history._stamp_foreign_nodes.
    judged = [r for r in records if r.get("foreign_node_ids") is not None]
    contaminated = [r for r in judged if r.get("contaminated")]
    n_judged = len(judged)
    contamination = {
        "records_with_gt": n_judged,
        "contaminated": len(contaminated),
        "pct": round(100.0 * len(contaminated) / n_judged, 1) if n_judged else None,
        "foreign_nodes_per_record": (
            round(sum(len(r["foreign_node_ids"]) for r in judged) / n_judged, 2) if n_judged else None
        ),
    }

    # ── resolve-slot skips (windowed, from the skip deque) ──────────────────
    # The counter in "counters" below is since-boot; these are the skips that
    # happened inside this window, so they can be read against the attempts in
    # the same window.  attempts_ratio is (all-lane skips / DARK attempts) —
    # the shape the acceptance target for the claim-on-publish fix is quoted
    # in (live baseline ~1 537 / 646 = 2.4), not a per-lane rate.  The dark
    # numerator is published beside it for anyone who wants one.
    all_skips = list(state.solver_resolve_skips_recent)
    skips = [s for s in all_skips if s["ts_ms"] >= cutoff_ms]
    resolve_skips = {
        "total": len(skips),
        "dark": sum(1 for s in skips if s["lane"] == "dark"),
        "attempts_ratio": round(len(skips) / attempts, 3) if attempts else None,
        # The skip deque is 500 entries against a live rate of ~50/min, so a
        # long window IS truncated here even when the solve-history stores
        # cover it.  Same honesty rule as window_effective_minutes above: read
        # it before reading total as a window count.
        "window_effective_minutes": _window_effective_minutes(all_skips, minutes),
    }

    pos_errors.sort()
    n_err = len(pos_errors)
    median_err = pos_errors[n_err // 2] if n_err else None
    p90_err = pos_errors[int(0.9 * (n_err - 1))] if n_err else None

    # ── known-lane position error (windowed) ────────────────────────────────
    # The lane's own error metric, so its accuracy is readable now that its
    # records no longer pass through the funnel above.  displacement_km, not
    # gt_error_km: the known lane's initial guess IS the ADS-B fix dead-
    # reckoned to the solve epoch, so displacement from it is exactly the
    # solver-vs-truth error the lane exists to measure (known_lane._attempt
    # says the same), and unlike gt_error_km it is populated against real
    # traffic where no synthetic trail exists.  Ghosts are deliberately
    # included — a ghost's error is the datum the regular pipeline's
    # displacement gate deletes.
    known_errors = sorted(r["displacement_km"] for r in known_records if r.get("displacement_km") is not None)
    n_known_err = len(known_errors)
    known_median = known_errors[n_known_err // 2] if n_known_err else None
    known_p90 = known_errors[int(0.9 * (n_known_err - 1))] if n_known_err else None

    # ── fragmentation ───────────────────────────────────────────────────────
    # Windowed, from the same published records the funnel above counts —
    # distinct published keys is the acceptance metric top-down claiming
    # exists to move: O(targets) instead of O(solves).  anchored_pct reads
    # anchor_key rather than an outcome filter so it reflects every attempt
    # this window, not just successful ones — solver.py stamps anchor_key on
    # rejects too (see _record_solve_history).
    published_records = [r for r in records if r.get("outcome") == "published"]
    key_counts: dict[str, int] = {}
    anchored_published = 0
    for r in published_records:
        k = r.get("solve_key")
        if k is not None:
            key_counts[k] = key_counts.get(k, 0) + 1
        if r.get("anchor_key"):
            anchored_published += 1
    counts_sorted = sorted(key_counts.values())
    n_keys = len(counts_sorted)
    spk_median = counts_sorted[n_keys // 2] if n_keys else None
    spk_p90 = counts_sorted[int(0.9 * (n_keys - 1))] if n_keys else None
    anchored_pct = round(100.0 * anchored_published / len(published_records), 1) if published_records else 0.0

    # ── node pool ───────────────────────────────────────────────────────────
    # "Could this round have solved the aircraft with more nodes than it did?"
    # pool_n_nodes is stamped on the solver input by the association stage (the
    # node set of the shared-track component the input was clustered out of, see
    # InterNodeAssociator._shared_track_pools), so it is the number of nodes
    # that PAIRED on this aircraft this round — the denominator n_nodes should
    # be read against.  narrower_than_pool counts published dark solves that
    # left at least one paired node out; the shortfall mean says how many.
    #
    # Records without the stamp are records the question does not apply to —
    # anchored/known-lane inputs and dark-follow predictions never went through
    # that clustering — so they are excluded from the denominator rather than
    # counted as zero shortfall.  records_with_pool against len(published_records)
    # is how much of the window the number actually speaks for.
    pooled = [r for r in published_records if r.get("pool_n_nodes") is not None]
    shortfalls = [int(r["pool_n_nodes"]) - int(r.get("n_nodes") or 0) for r in pooled]
    narrower = sum(1 for d in shortfalls if d > 0)

    # ── ghosts (DARK tracks only) ───────────────────────────────────────────
    # The question this answers is "of the multinode tracks we put on the map
    # with no transponder to lean on, what fraction are real?", so an
    # ADS-B-associated track is not evidence either way and must not sit in the
    # denominator.  It used to: ghost_tracks and gt_matched were both counted
    # after ``continue``-ing every ADS-B track, but precision_pct divided by
    # live_tracks — so a fleet of tagged tracks with no dark track at all
    # scored a flat 100 % precision and a permanently 0 gt_matched, which is
    # the shape a working dark lane and a completely dead one share.  With no
    # dark tracks the honest answer is None, not 100.
    #
    # dark_tracks partitions exactly: gt_matched + adsb_near + ghost_tracks.
    # adsb_near is the real-traffic rescue kept as its own line rather than
    # folded into "not a ghost" — on the test droplet real adsb.lol traffic
    # pollutes the dark candidate pool, and how much of dark precision rests
    # on that gate rather than on ground truth is the thing worth watching.
    now = time.time()
    now_ms = now * 1000.0
    # list() before iterating, in all three cases: these dicts are written by
    # the frame workers, the sim ingest and the solver threads while this
    # request runs.  ground_truth_trails/adsb_aircraft follow core/state.py's
    # unlocked list(dict) snapshot idiom; multinode_tracks takes the lock the
    # solver writes it under (state.multinode_tracks_lock, also honoured by
    # known_lane._publish) because a plain iteration here raced its inserts
    # and 500'd the endpoint with "dictionary changed size during iteration".
    gt_trails = [(hx, list(trail)) for hx, trail in list(state.ground_truth_trails.items())]
    fresh_adsb = [
        a
        for a in list(state.adsb_aircraft.values())
        if a.get("last_seen_ms") is not None and now_ms - a["last_seen_ms"] <= _ADSB_FRESH_S * 1000.0
    ]
    with state.multinode_tracks_lock:
        mn_tracks = list(state.multinode_tracks.items())

    live_tracks = 0
    adsb_associated = 0
    dark_tracks = 0
    gt_matched = 0
    adsb_near = 0
    ghost_tracks = 0
    for key, rec in mn_tracks:
        live_tracks += 1
        if rec.get("adsb_hex") or key.startswith("mn-adsb-"):
            adsb_associated += 1
            continue
        lat, lon = rec.get("lat"), rec.get("lon")
        if lat is None or lon is None:
            # Unclassifiable, so out of the denominator entirely rather than
            # scored as a ghost; a published entry always carries a position.
            continue
        dark_tracks += 1

        matched = False
        for _hx, trail in gt_trails:
            if not trail:
                continue
            pt = min(trail, key=lambda p: abs(p[3] - now))
            if abs(pt[3] - now) > _GHOST_GT_MAX_AGE_S:
                continue
            if haversine_km(lat, lon, pt[0], pt[1]) <= _GHOST_GATE_KM:
                matched = True
                break
        if matched:
            gt_matched += 1
            continue

        if any(haversine_km(lat, lon, a["lat"], a["lon"]) <= _GHOST_GATE_KM for a in fresh_adsb):
            adsb_near += 1
        else:
            ghost_tracks += 1

    precision_pct = round((dark_tracks - ghost_tracks) / dark_tracks * 100, 1) if dark_tracks else None
    live_ghosts = {
        "live_tracks": live_tracks,
        # Informational — excluded from the precision denominator.
        "adsb_associated": adsb_associated,
        "dark_tracks": dark_tracks,
        "gt_matched": gt_matched,
        "adsb_near": adsb_near,
        "ghost_tracks": ghost_tracks,
        "precision_pct": precision_pct,
    }
    windowed_ghosts = _windowed_ghosts(published_records)

    # ── known lane / claiming counters ──────────────────────────────────────
    # One counters_lock acquisition for both blocks below, so the two are a
    # single consistent snapshot rather than ten reads interleaved with the
    # frame and solver workers bumping them.
    with state.counters_lock:
        kl_attempts = state.known_lane_attempts
        kl_truth_match = state.known_lane_truth_match
        kl_ghost = state.known_lane_ghost
        kl_no_converge = state.known_lane_no_converge
        kl_published = state.known_lane_published
        kl_publish_errors = state.known_lane_publish_errors
        kl_reanchored = state.known_lane_reanchored
        kl_publish_rms_rejected = state.known_lane_publish_rms_rejected
        kc_made = state.known_claims_made
        kc_contentions = state.known_claim_contentions
        kc_bound = state.known_claims_bound
        kc_visibility_rejects = state.known_claims_visibility_rejects
        kc_world_rejects = state.known_claims_world_rejects
        kc_errors = state.known_claims_errors
        kh_claims = state.known_hold_claims
        kh_expired = state.known_hold_expired
        kh_disagree = state.known_hold_dropped_disagree
        kf_claims = state.known_follow_claims
        cal_recorded = state.calibration_points_recorded
        cal_rejects = {
            reason: getattr(state, f"calibration_claims_rejected_{reason}")
            for reason in ("hold", "stale_fix", "residual", "contested", "immature")
        }
        # Same one-lock snapshot for the follow lane's funnel and the
        # per-reason ineligibility tally beside it: the two are only readable
        # against each other (see the dark_follow block below), so they must
        # not be sampled a rebuild apart.
        df_targets = state.dark_follow_targets
        df_inputs = state.dark_follow_inputs
        df_published = state.dark_follow_published
        df_dropped = state.dark_follow_dropped
        df_n2_withheld = state.dark_follow_n2_withheld
        df_n2_skipped = state.dark_follow_n2_skipped
        df_kept_manoeuvre = state.dark_follow_kept_manoeuvre
        df_gate_clamped = state.dark_follow_gate_sigma_clamped
        df_inelig = {
            reason: getattr(state, f"dark_follow_inelig_{reason}")
            for reason in (
                "cooldown",
                "no_pos",
                "age",
                "min_solves",
                "min_nodes",
                "no_filter",
                "vel_sigma",
            )
        }

    return {
        "window_minutes": minutes,
        # How much of window_minutes the solve-history stores actually hold.
        # Lower than window_minutes means the answer is truncated (short
        # uptime, or a deque at its maxlen) — see _window_effective_minutes.
        "window_effective_minutes": effective_minutes,
        # Denominator for everything above: how many records in this window
        # each lane wrote.  Sums to the whole merged window, so the dark-lane
        # funnel below can be read against what it excludes.
        "lane_split": lane_split,
        "by_world": _world_funnels(all_records),
        "evaluation_note": "known/adsb lanes use ADS-B inputs; blind replay is reported separately",
        # ── DARK LANE ONLY, from here to fragmentation ──────────────────────
        "attempts": attempts,
        "published": {"total": n2 + n3plus, "n2": n2, "n3plus": n3plus},
        "rejects": {"total": reject_total, "by_reason": by_reason},
        "position_error_km": {"median": median_err, "p90": p90_err, "n": n_err},
        # The funnel again, split by how many nodes each attempt had — the
        # dark lane's behaviour is not uniform in n and the aggregate hides
        # it.  Same window and same records as attempts/published/rejects
        # above, so the buckets sum back to them.  See _by_n_nodes.
        "by_n_nodes": _by_n_nodes(records),
        # ...and the same table for the known lane's own records, which do not
        # pass through the funnel above (see the docstring).
        "by_n_nodes_known": _by_n_nodes(known_records),
        # Both windowed and both DARK-lane, like the funnel above them.
        "contamination": contamination,
        "resolve_skips": resolve_skips,
        "ghosts": {
            # Windowed over the funnel's published dark records (see the
            # docstring); precision_pct is None (not 100.0) when nothing in
            # the window could be judged.  ``live`` is the point-in-time scan
            # of state.multinode_tracks the block used to consist of.
            "scope": "dark",
            "gate_km": _GHOST_GATE_KM,
            **windowed_ghosts,
            "live": live_ghosts,
        },
        "consensus": {
            "mode": solver_mod._CONSENSUS_MODE,
            "selected": state.solver_consensus_selected,
            "filtered": state.solver_consensus_filtered,
            "fallback": state.solver_consensus_fallback,
            "shadow": state.solver_consensus_shadow,
        },
        # Top-down claiming, since boot — same "cumulative regardless of
        # mode" convention as consensus above.  rounds/matched/conflicts/
        # anchored_inputs/tracklets_excluded come off the library associator
        # (the claiming stage itself); anchor_hits/anchor_fallbacks/
        # anchored_published are the solver-side honoring outcome.
        "claiming": {
            "mode": state.node_associator.claim_mode,
            "rounds": state.node_associator.claim_rounds,
            "matched": state.node_associator.claims_matched,
            "conflicts": state.node_associator.claim_conflicts,
            "anchored_inputs": state.node_associator.anchored_inputs_emitted,
            "tracklets_excluded": state.node_associator.tracklets_excluded,
            "anchor_hits": state.solver_anchor_hits,
            "anchor_fallbacks": state.solver_anchor_fallbacks,
            "anchored_published": state.solver_anchored_published,
        },
        # Empirical FOV beam gate (FOV_MODE) — since-boot counters, same
        # "cumulative regardless of mode" convention as claiming/consensus
        # above.  See solver.py's beam gate for what agree/would_pass/
        # would_reject mean.
        "fov": {
            "mode": state.FOV_MODE,
            "shadow_agree": state.fov_shadow_agree,
            "would_pass": state.fov_shadow_would_pass,
            "would_reject": state.fov_shadow_would_reject,
            "neg_events": state.fov_neg_events,
        },
        # Known-lane solver (KNOWN_LANE_MODE) — since-boot counters, same
        # "cumulative regardless of mode" convention as claiming/consensus/fov
        # above.  attempts is every claimed hex the lane tried; truth_match /
        # ghost / no_converge partition those attempts by outcome (see
        # services/tasks/known_lane.py); published is the truth_match subset
        # binding mode actually put on the map, and stays zero in shadow.
        # publish_errors nonzero means solves classified truth_match failed to
        # reach the feed — it accounts for exactly the truth_match minus
        # published discrepancy that otherwise only the logs can explain.
        "known_lane": {
            "mode": state.KNOWN_LANE_MODE,
            "attempts": kl_attempts,
            "truth_match": kl_truth_match,
            "ghost": kl_ghost,
            "no_converge": kl_no_converge,
            "published": kl_published,
            "publish_errors": kl_publish_errors,
            # Ghost solves the lane re-anchored onto instead (see
            # known_lane._reanchor): the kf-seeded prior had drifted — the
            # aircraft turned while silent — and two consecutive solves agreed
            # with each other rather than with it.  They are published like a
            # truth_match and are NOT part of the ghost count.
            "reanchored": kl_reanchored,
            # Solves binding WOULD have published, held off the map because
            # their rms_delay failed the regular lane's bound (see
            # known_lane.KNOWN_PUBLISH_MAX_RMS_DELAY_US).  Like publish_errors
            # it accounts for part of the truth_match+reanchored minus
            # published gap, and it stays zero in shadow — nothing was going to
            # publish there.  The withheld solves are still classified, still
            # sampled and still in position_error_km below: the gate protects
            # the map, not the measurement.
            "publish_rms_rejected": kl_publish_rms_rejected,
            # The one WINDOWED entry in this since-boot block (it carries its
            # own window_minutes so it cannot be misread as cumulative):
            # solver-vs-ADS-B error over this lane's records in the window,
            # from displacement_km.  truth_match and ghost are both included —
            # see the known_errors comment above.
            "position_error_km": {
                "median": known_median,
                "p90": known_p90,
                "n": n_known_err,
                "window_minutes": minutes,
            },
        },
        # Claiming stage feeding the lane above (services/known_claiming.py),
        # also since boot.  made counts every claim recorded in either mode;
        # bound the detections binding actually removed from the dark pool, so
        # made > 0 with bound == 0 is the shadow-soak signature.  errors
        # nonzero means the claiming stage is throwing and the lane is
        # silently contributing nothing.
        # Dark track following (services/dark_follow.py), since boot except
        # targets_now, a live gauge of the current pseudo-state list.  The
        # funnel repeats three of the "counters" entries below because it is
        # only readable beside "ineligible", which is the other half of the
        # same walk: every dark key in state.multinode_tracks is either a
        # target or one of those reasons, re-tested on every rebuild.
        #
        # ineligible IS IN KEY-SECONDS, the funnel is in events.  The target
        # list is rebuilt once a second and every dark key is re-tested, so a
        # key that is ineligible for a minute adds ~60 to its reason.  Compare
        # the reasons with each other (which gate holds the lane back) and
        # with targets_now; do NOT divide them by inputs or published.
        "dark_follow": {
            "mode": dark_follow.mode(),
            "targets_now": df_targets,
            "inputs": df_inputs,
            "published": df_published,
            "dropped": df_dropped,
            # The two n=2 sparings, in events like the rest of the funnel.
            # n2_skipped is the follow input never built because the claim
            # round matched only two nodes and DARK_FOLLOW_N2_ADMIT is off;
            # n2_withheld is the n2_unconfirmed verdict that was not charged to
            # the key's reject streak.  Both are subtractions from "dropped"
            # and from ineligible["cooldown"] — the bucket they were the
            # dominant source of — so read them beside those two.
            "n2_withheld": df_n2_withheld,
            "n2_skipped": df_n2_skipped,
            # The manoeuvre reprieve (DARK_FOLLOW_MANOEUVRE_KEEP).
            # kept_manoeuvre is IN KEY-SECONDS like "ineligible" beside it —
            # the over-ceiling time a turn explained, so it is the subtraction
            # from ineligible["vel_sigma"] and, downstream of it, from
            # "dropped".  gate_sigma_clamped is in EVENTS (one per gate pair
            # computed at DARK_FOLLOW_GATE_VEL_SIGMA_CAP_MS instead of the
            # filter's inflated sigma), and is the half of the trade that says
            # the kept keys are still claiming at the old gate width.
            "kept_manoeuvre": df_kept_manoeuvre,
            "gate_sigma_clamped": df_gate_clamped,
            "ineligible": df_inelig,
        },
        "known_claims": {
            "made": kc_made,
            "contentions": kc_contentions,
            "bound": kc_bound,
            "visibility_rejects": kc_visibility_rejects,
            "world_rejects": kc_world_rejects,
            "errors": kc_errors,
            # Path H (services/known_claiming._claim_holds).  claims is the
            # detections held onto a hex after its transponder stopped
            # explaining them; disagree the ghost-lock guard firing (a fresh
            # fix contradicted the held track); holds the CURRENT size of the
            # store, a gauge, summed over nodes — read beside claims, since a
            # store that grows while claims does not is holds that never match.
            "hold_claims": kh_claims,
            "hold_expired": kh_expired,
            "hold_dropped_disagree": kh_disagree,
            # Follow claims (services/known_claiming._follow_states): claims a
            # node made against the lane's own published position for a hex
            # whose transponder went stale, having no hold of its own — the
            # detections that would otherwise have started a dark twin.
            "follow_claims": kf_claims,
            "holds": sum(len(h) for h in list(state.known_track_holds.values())),
            # Empirical-coverage calibration, which under KNOWN_LANE_MODE != off
            # comes only from this lane (services/calibration.py's fourth rule).
            # recorded is points written; rejected is the five rules, charged in
            # order — exactly one per non-hold claim, so they sum with recorded
            # to the non-hold claim count.
            "calibration_recorded": cal_recorded,
            "calibration_rejected": cal_rejects,
        },
        # Dark published solves against the node pool their round had for the
        # same aircraft (see the pooled/shortfalls block above).  pct is null
        # rather than 0 when nothing in the window carried the stamp, because
        # "no solve was narrower than its pool" and "no solve was measured" are
        # not the same answer.
        "pool": {
            "records_with_pool": len(pooled),
            "narrower_than_pool": narrower,
            "pct": round(100.0 * narrower / len(pooled), 1) if pooled else None,
            "mean_shortfall_nodes": round(sum(shortfalls) / len(shortfalls), 3) if shortfalls else None,
        },
        "fragmentation": {
            "distinct_keys": len(key_counts),
            "published": len(published_records),
            "solves_per_key": {"median": spk_median, "p90": spk_p90},
            "anchored_pct": anchored_pct,
            # Dark-lane key decisions, SINCE BOOT (the claiming/consensus/fov
            # convention above) rather than windowed like the four keys before
            # them — they come off state counters, not the record window.  A
            # birth (minted) against a re-key (proximity) is the decision the
            # age-scaled proximity gate makes; distinct_keys above is what
            # survived those decisions inside the window, so the two answer
            # different halves of the same question and are both worth having
            # here.  Per-record key_how/key_dist_km in mlat_solve_history is
            # the windowed version when one is needed.
            "dark_keys_minted": state.solver_key_minted_dark,
            "dark_keys_proximity": state.solver_key_proximity_dark,
            # ...and the re-keys the node-track evidence decided rather than
            # distance alone (multinode_identity.py's TRACK_LINK_AGE_S) — a
            # shared tracker track id inside the gate, including the
            # follow-owned keys that are joined on two of them.  Each one is a
            # key birth the distance-only rule would have made, or a solve it
            # would have discarded.
            "dark_keys_tracks": state.solver_key_tracks,
            # ...and how many of those re-keys matched an entry measured
            # AFTER the solve that joined it (signed dt < 0).  Those entries
            # were invisible to the scan until _MN_ASSOC_MAX_NEG_DT_S, so
            # this number is the fragmentation the signed window reclaims —
            # every one of them was a dark_keys_minted before.
            "dark_keys_proximity_negdt": state.solver_key_proximity_negdt,
            # Supersession, also since boot: entries popped because a new
            # solve was judged to be the same aircraft (multinode_identity.py's
            # _supersession_match), against entries that shared a source
            # track id with it and were refused.  These belong beside the key
            # decisions because they are the other half of the same
            # fragmentation loop — a wrong pop deletes a live aircraft's key
            # and its next solve shows up above as another dark_keys_minted.
            # Tracker track ids are shared across candidates for different
            # aircraft, so a large blocked count is the guard working, not a
            # fault.
            # ...and, of the refusals, the ones the altitude half of the gate
            # made on its own: close enough to pop, thousands of metres apart
            # vertically.  That is the neighbour-pop signature (a bad solve of
            # one aircraft landing on another's key), so this is the counter
            # that says whether the altitude test is earning its place.
            "mn_superseded": state.mn_superseded,
            "mn_superseded_blocked": state.mn_superseded_blocked,
            "mn_superseded_blocked_alt": state.mn_superseded_blocked_alt,
            # Mint-time retirement of coasting keys (MN_STALE_COAST_ENABLED).
            # The hard-turn re-key the shared-id prefilter above cannot see.
            # retired + the three blocked/none counters sum to the dark mints
            # (solver_key_minted_dark) taken while the feature was enabled, so
            # "retired" is readable as a fraction: on the captures this was
            # built from, roughly half of dark re-keys around a turn left a
            # ghost worth retiring.  blocked_evidence is the one to watch —
            # it is every retirement proximity alone would have made.
            "mn_stale_coast_retired": state.mn_stale_coast_retired,
            "mn_stale_coast_blocked_alt": state.mn_stale_coast_blocked_alt,
            "mn_stale_coast_blocked_evidence": state.mn_stale_coast_blocked_evidence,
            "mn_stale_coast_none": state.mn_stale_coast_none,
        },
        # Display smoother (services/track_filter.py), since boot except
        # manoeuvre_active, which is a live gauge.  reanchors are chi-squared
        # gate breaches that the manoeuvre retry could NOT explain — genuine
        # identity breaks; manoeuvre_rescues are the ones it could, which
        # before the adaptive process noise existed were counted in the first
        # group and were most of it (every turn past ~60-110 degrees produced
        # one).  A rising re-anchor count with rescues near zero means the
        # manoeuvre sigma is too small for the turns being flown.
        "display_filter": track_filter.filter_stats(),
        "counters": {
            "successes": state.solver_successes,
            "failures": state.solver_failures,
            # Pool round trips abandoned at SOLVER_POOL_CALL_TIMEOUT_S and
            # retried inline.  A stuck-but-alive child used to hold one of the
            # two worker threads forever with every counter reading healthy.
            "pool_timeouts": state.solver_pool_timeouts,
            "n2_unconfirmed": state.n2_unconfirmed,
            # n=2 solves published without a constant-velocity fit because an
            # anchored follow input vouched for the pairing (dark_follow.
            # DARK_FOLLOW_N2_ADMIT).  Read against n2_unconfirmed: this is the
            # share of the n=2 gate the follow lane is now walking past.
            "n2_anchored_admitted": state.n2_anchored_admitted,
            "n2_fit_position_published": state.n2_fit_position_published,
            "solver_trimmed": state.solver_trimmed,
            "stale_drops": state.solver_stale_drops,
            "resolve_skips": state.solver_resolve_skips,
            "tracks_stale_skipped": state.tracks_stale_skipped,
            "epoch_align_skipped": state.solver_epoch_align_skipped,
            # Dark share of the line above.  The windowed version, with the
            # blocking claims, is the "resolve_skips" block further up.
            "resolve_skips_dark": state.solver_resolve_skips_dark,
            # Candidates admitted by the 3+-node refresh rule that the width
            # rule alone would have skipped (solver.py's
            # _SOLVER_RESOLVE_REFRESH_S).  Extra solves bought on purpose, so
            # that an entry nothing else refreshes stops dead-reckoning the
            # whole 12 s window.
            "resolve_refresh": state.solver_resolve_refresh,
            # Pool adoption (solver.py's _adopt_pool_nodes): dark candidates
            # solved narrower than the round's node pool, how many were
            # re-solved wider once the narrow solve vouched for the extra
            # node's delay/Doppler, and how many node-measurements that added.
            "adopt_eligible": state.solver_adopt_eligible,
            "adopt_widened": state.solver_adopt_widened,
            "adopt_nodes_added": state.solver_adopt_nodes_added,
            "adopt_rejected": state.solver_adopt_rejected,
            "queue_drops": state.solver_queue_drops,
            # Frames the per-node rate limiter refused before the tracker ever
            # saw them (tcp_handler's NODE_FRAME_MIN_INTERVAL_S).  Not the
            # same event as /api/admin/metrics' frames_dropped, which is
            # frame_queue saturation and normally reads zero.
            "node_frames_rate_limited": state.node_frames_rate_limited,
            "worker_errors": state.solver_worker_errors,
            "vel_untrusted_published": state.solver_vel_untrusted_published,
            # n=2 inputs whose initial-guess altitude came from an established
            # 3+-node dark key rather than the association grid (solver.py's
            # _inherit_key_altitude).  The per-solve evidence is alt_source on
            # the history records; this is the since-boot rate.
            "n2_alt_inherited": state.solver_n2_alt_inherited,
            # Dark track following (services/dark_follow.py), since boot except
            # targets, which is a live gauge of the current pseudo-state list.
            # The funnel is targets -> claims -> inputs -> published; dropped is
            # the ghost guard's tally of keys it stopped following.
            "dark_follow_targets": state.dark_follow_targets,
            "dark_follow_claims": state.dark_follow_claims,
            "dark_follow_inputs": state.dark_follow_inputs,
            "dark_follow_published": state.dark_follow_published,
            "dark_follow_dropped": state.dark_follow_dropped,
            "dark_follow_n2_withheld": state.dark_follow_n2_withheld,
            "dark_follow_n2_skipped": state.dark_follow_n2_skipped,
            # The other side of the lane: bottom-up dark solves refused at
            # keying because the follow lane owns the key they landed on.  It
            # belongs beside the funnel because it is the same trade — the
            # lane keeps a key only if it also stops the bottom-up lane from
            # corrupting it — and the matching per-solve records are the
            # "shadowed_by_follow" entries in rejects.by_reason.
            "dark_bottomup_shadowed": state.dark_bottomup_shadowed,
        },
    }
