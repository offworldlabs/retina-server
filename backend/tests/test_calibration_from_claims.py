"""Empirical-coverage calibration from the CLAIM lane — the five rules in
services/known_claiming._calibration_from_claim, and the emit-loop path they
replace whenever KNOWN_LANE_MODE is not "off".

Geometry, cache seeding and frame shapes are reused verbatim from
test_known_claiming.py: the rules under test are a filter ON claiming, so they
have to be exercised through the same claiming the rest of that file pins.
The one local addition is `_register`, which puts the node into
state.node_analytics as well as the associator — without an
EmpiricalCoverageState there is nothing for a point to land in, and every
assertion here reads
``state.node_analytics.empirical_coverages[node].n_points``.
"""

import time

import pytest
from retina_analytics.association import predict_observation
from retina_analytics.constants import offset_latlon_m
from retina_analytics.empirical_coverage import _bearing_and_range, _bin_for_bearing

from config.constants import (
    CAL_CLAIM_DELAY_US,
    CAL_CLAIM_DOPPLER_HZ,
    CAL_CLAIM_MIN_CLAIMS,
    CAL_CLAIM_STREAK_GAP_S,
    CAL_MAX_ADSB_AGE_S,
    FT_TO_M,
)
from core import state
from services import known_claiming as kc
from tests.node_helpers import register_test_node
from tests.test_known_claiming import (
    _ALT_BARO_FT,
    _LAT,
    _LON,
    _NODE_CFG,
    _cache_state,
    _frame,
    _stationary_pred,
)

_NODE_ID = "test-cal-from-claims"
# One frame per second, the fleet's cadence — every streak in this file is
# built at it, so no gap here ever reaches CAL_CLAIM_STREAK_GAP_S by accident.
_FRAME_DT_MS = 1000


def _register(node_id=_NODE_ID):
    """Register with analytics AND the associator, the way an entry point does."""
    register_test_node(node_id, dict(_NODE_CFG, node_id=node_id))
    return state.node_associator.node_geometries[node_id]


def _n_points(node_id=_NODE_ID):
    ec = state.node_analytics.empirical_coverages.get(node_id)
    return ec.n_points if ec is not None else 0


def _claim_frames(
    n,
    ts0,
    delays,
    dopplers,
    node_id=_NODE_ID,
    adsb=None,
    refresh_fix=True,
    hexes=("aaa111",),
    dt_ms=_FRAME_DT_MS,
    **cache_kwargs,
):
    """Run n consecutive frames, re-stamping the cached fixes on each.

    A calibration point needs a MATURE link, so nothing in this file can be
    tested on a single frame; this is the shared "claim the same hex n times
    in a row" driver.  refresh_fix=False leaves the fix where it was, which is
    how the stale-fix rule is reached.
    """
    for k in range(n):
        ts = ts0 + k * dt_ms
        if refresh_fix:
            for h in hexes:
                _cache_state(h, ts, **cache_kwargs.get(h, {}))
        kc.claim_known_targets(node_id, _frame(ts, delays, dopplers, adsb=adsb))


@pytest.fixture
def _binding(monkeypatch):
    monkeypatch.setattr(state, "KNOWN_LANE_MODE", "binding")


@pytest.fixture
def _no_holds(monkeypatch):
    """Disable path H (the same rollback lever KNOWN_HOLD_MAX_GAP_S=0 is in
    production) for the tests that exercise PATH 2.

    Not a convenience.  Path H outranks path 2, and every claim — path 2's
    included — creates a hold, so from the SECOND frame of a link onwards a
    node with no tags is claiming through path H, which rule 1 refuses.  That
    interaction is pinned by TestHoldAndFollow.test_holds_outrank_path_2_from
    _the_second_frame below rather than hidden here; these tests are about the
    other four rules and need path 2 to keep running to reach them.
    """
    monkeypatch.setattr(kc, "KNOWN_HOLD_MAX_GAP_S", 0.0)


class TestMaturityAndTheCleanCase:
    def test_records_on_the_third_claim_and_not_before(self, _binding, _no_holds):
        """The happy path: one aircraft, no rival, zero residual, fresh fix.
        Nothing is recorded until the link is CAL_CLAIM_MIN_CLAIMS claims old,
        and the third claim itself records — maturity gates the sample, it
        does not spend it."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)

        for k in range(CAL_CLAIM_MIN_CLAIMS):
            _claim_frames(1, ts0 + k * _FRAME_DT_MS, [pd], [pf])
            expected = 1 if k + 1 >= CAL_CLAIM_MIN_CLAIMS else 0
            assert _n_points() == expected, f"after claim {k + 1}"

        assert state.calibration_points_recorded == 1
        assert state.calibration_claims_rejected_immature == CAL_CLAIM_MIN_CLAIMS - 1

    def test_keeps_recording_once_mature(self, _binding, _no_holds):
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)
        _claim_frames(6, ts0, [pd], [pf])
        assert _n_points() == 6 - (CAL_CLAIM_MIN_CLAIMS - 1)

    def test_streak_resets_after_a_long_gap(self, _binding, _no_holds):
        """A gap over CAL_CLAIM_STREAK_GAP_S is a different link, so the count
        restarts — two claims, a gap, and two more record nothing."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)

        _claim_frames(2, ts0, [pd], [pf])
        assert _n_points() == 0

        gap_ms = int((CAL_CLAIM_STREAK_GAP_S + 2.0) * 1000)
        _claim_frames(2, ts0 + 2 * _FRAME_DT_MS + gap_ms, [pd], [pf])
        assert _n_points() == 0, "the gap must have restarted the count at 1"

        # ...and the third claim of the NEW streak records, proving the store
        # restarted rather than froze.
        _claim_frames(1, ts0 + 4 * _FRAME_DT_MS + gap_ms, [pd], [pf])
        assert _n_points() == 1

    def test_a_gap_inside_the_window_does_not_reset(self, _binding, _no_holds):
        """Dropped frames are routine (the simulator's miss rate reaches 40%
        at threshold), so a gap the window tolerates must keep the streak."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)
        gap_ms = int((CAL_CLAIM_STREAK_GAP_S - 2.0) * 1000)
        for k in range(CAL_CLAIM_MIN_CLAIMS):
            _claim_frames(1, ts0 + k * gap_ms, [pd], [pf])
        assert _n_points() == 1


class TestExclusivity:
    def test_a_rival_inside_the_claim_gate_blocks_the_point(self, _binding, _no_holds, monkeypatch):
        """A second cached aircraft whose prediction lands inside the claim
        gate of this detection: the lane still claims (identity evidence is
        what it is for), but the attribution is a coin-toss and must not
        characterize coverage."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        # bbb222 sits 1 µs / 2 Hz away — inside the 10 µs / 25 Hz claim gate,
        # so it is a rival, and its own residual against the detection is
        # small enough that only exclusivity can reject this.
        monkeypatch.setattr(
            kc,
            "predict_observation",
            lambda g, lat, lon, alt_km, ve=0.0, vn=0.0, vu=0.0: (pd, pf) if lat <= _LAT else (pd + 1.0, pf + 2.0),
        )
        ts0 = int(time.time() * 1000)
        _claim_frames(
            CAL_CLAIM_MIN_CLAIMS + 2,
            ts0,
            [pd],
            [pf],
            hexes=("aaa111", "bbb222"),
            **{"bbb222": {"lat": _LAT + 0.02}},
        )

        assert state.known_claims_made > 0, "the lane must still have claimed"
        assert _n_points() == 0
        assert state.calibration_claims_rejected_contested >= 1

    def test_a_rival_outside_the_claim_gate_does_not(self, _binding, _no_holds, monkeypatch):
        """The control for the test above: same two aircraft, the second moved
        beyond the claim gate, and the point is recorded."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        monkeypatch.setattr(
            kc,
            "predict_observation",
            lambda g, lat, lon, alt_km, ve=0.0, vn=0.0, vu=0.0: (pd, pf) if lat <= _LAT else (pd + 40.0, pf + 200.0),
        )
        ts0 = int(time.time() * 1000)
        _claim_frames(
            CAL_CLAIM_MIN_CLAIMS,
            ts0,
            [pd],
            [pf],
            hexes=("aaa111", "bbb222"),
            **{"bbb222": {"lat": _LAT + 0.02}},
        )
        assert _n_points() == 1

    def test_dark_projection_contention_blocks_the_point(self, _binding, _no_holds, monkeypatch):
        """The existing `contested` flag — an established dark global whose
        projection also explains this detection — is the other half of rule 4."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)
        monkeypatch.setattr(kc, "_dark_global_projections", lambda g, t: [(pd, pf)])

        _claim_frames(CAL_CLAIM_MIN_CLAIMS + 1, ts0, [pd], [pf])

        assert state.known_claim_contentions > 0
        assert _n_points() == 0
        assert state.calibration_claims_rejected_contested >= 1


class TestResidualAndFreshness:
    def test_residual_inside_the_claim_gate_but_beyond_the_calibration_gate(self, _binding, _no_holds):
        """The rule that does the real work: a claim the lane is happy to make
        at 10 µs is not a claim the polygon should inherit."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)
        off = CAL_CLAIM_DELAY_US + 1.0  # inside KNOWN_CLAIM_DELAY_GATE_US (10)

        _claim_frames(CAL_CLAIM_MIN_CLAIMS + 1, ts0, [pd + off], [pf])

        assert state.known_claims_made > 0
        assert _n_points() == 0
        assert state.calibration_claims_rejected_residual >= 1

    def test_doppler_residual_beyond_the_calibration_gate(self, _binding, _no_holds):
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)

        _claim_frames(CAL_CLAIM_MIN_CLAIMS + 1, ts0, [pd], [pf + CAL_CLAIM_DOPPLER_HZ + 2.0])

        assert state.known_claims_made > 0
        assert _n_points() == 0
        assert state.calibration_claims_rejected_residual >= 1

    def test_stale_fix_inside_the_claim_age_cap(self, _binding, _no_holds):
        """Between CAL_MAX_ADSB_AGE_S (10 s) and KNOWN_CLAIM_MAX_FIX_AGE_S
        (45 s) the lane still claims — dead reckoning is what the age-scaled
        gate is for — but the position is too old to characterize coverage."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        fix_ts = int(time.time() * 1000)
        _cache_state("aaa111", fix_ts)
        ts0 = fix_ts + int((CAL_MAX_ADSB_AGE_S + 5.0) * 1000)

        _claim_frames(CAL_CLAIM_MIN_CLAIMS + 1, ts0, [pd], [pf], refresh_fix=False)

        assert state.known_claims_made > 0
        assert _n_points() == 0
        assert state.calibration_claims_rejected_stale_fix >= 1


class TestHoldAndFollow:
    """Path H, and the BLIND node this feature is really for.

    Path H's precedence is what decides how much calibration a node with no
    ADS-B tags yields.  Every claim creates a hold (_touch_hold is called for
    all of them) and _claim_holds runs BEFORE path 2, so a link established by
    path 2 is claimed by path H on every subsequent frame.  Refusing all of
    those under rule 1 left such a link permanently immature — measured at
    0.12 points per node-minute against the ~50 a tagged node gets — so rule 1
    now refuses only a FOLLOW claim and a hold with no live transponder behind
    it.  The three tests below are the two sides of that split and the residual
    rule that keeps the refreshed side honest.
    """

    # The claim-lane geometry the refreshed-hold tests share: a fix 8 s old at
    # every frame, 400 kt due east, so the dead-reckoned position is ~1.6 km
    # (several 5° bins at this range) from the reported one and "which
    # position was recorded" has an observable answer.
    _FIX_AGE_S = 8.0
    _GS_KT, _TRACK_DEG = 400.0, 90.0

    def _moving_fix(self, geo):
        """(dr_lat, dr_lon, pred_delay, pred_doppler) for that aircraft."""
        ve = self._GS_KT * 0.514444
        dr_lat, dr_lon = offset_latlon_m(_LAT, _LON, east_m=ve * self._FIX_AGE_S, north_m=0.0)
        pd, pf = predict_observation(geo, dr_lat, dr_lon, _ALT_BARO_FT * FT_TO_M / 1000.0, ve, 0.0)
        return dr_lat, dr_lon, pd, pf

    def _run_blind(self, n_frames, ts0, pd, pf, node_id=_NODE_ID):
        """n consecutive tagless frames on a fix re-stamped _FIX_AGE_S back."""
        for k in range(n_frames):
            ts = ts0 + k * _FRAME_DT_MS
            _cache_state("aaa111", ts - int(self._FIX_AGE_S * 1000), gs=self._GS_KT, track=self._TRACK_DEG)
            kc.claim_known_targets(node_id, _frame(ts, [pd], [pf]))

    def test_a_refreshed_hold_records_like_a_path_2_claim(self, _binding):
        """A node that sends no frame["adsb"] claims by path 2 once and by
        path H forever after, and every one of those holds is REFRESHED — the
        consistency rule in _claim_holds compared the same detection against
        the live cached fix inside path 2's own gate before letting the claim
        stand.  So the streak advances through them and the point lands at the
        FRESH FIX's dead-reckoned position, exactly as a path-2 claim's would.
        """
        geo = _register()
        dr_lat, dr_lon, pd, pf = self._moving_fix(geo)
        ts0 = int(time.time() * 1000)
        n_frames = 5

        self._run_blind(n_frames, ts0, pd, pf)

        assert state.known_hold_claims == n_frames - 1, "every claim after the first is path H's"
        assert state.calibration_claims_rejected_hold == 0
        assert state.calibration_claims_rejected_immature == CAL_CLAIM_MIN_CLAIMS - 1
        assert _n_points() == n_frames - (CAL_CLAIM_MIN_CLAIMS - 1)
        assert state.calibration_points_recorded == n_frames - (CAL_CLAIM_MIN_CLAIMS - 1)

        # ...and at the dead-reckoned position, not the reported one: a
        # refreshed hold carries path 2's dead reckoning, not the hold's.
        ec = state.node_analytics.empirical_coverages[_NODE_ID]
        dr_bearing, dr_range = _bearing_and_range(geo.rx_lat, geo.rx_lon, dr_lat, dr_lon)
        rep_bearing, _rep_range = _bearing_and_range(geo.rx_lat, geo.rx_lon, _LAT, _LON)
        assert _bin_for_bearing(dr_bearing) != _bin_for_bearing(rep_bearing), (
            "the test is only meaningful if the two positions land in different bins"
        )
        recorded = ec._bins[_bin_for_bearing(dr_bearing)]
        assert len(recorded) == ec.n_points
        assert recorded[0] == pytest.approx(dr_range, abs=0.05)
        assert ec._bins[_bin_for_bearing(rep_bearing)] == []

    def test_a_refreshed_hold_is_judged_on_the_fresh_fixs_residual(self, _binding, monkeypatch):
        """Rule 3 for a refreshed hold reads the TRANSPONDER's prediction, not
        the hold's.

        A hold predicts this node's next measurement from this node's last
        one, so its residual is near zero by construction whatever aircraft is
        actually out there — judging calibration on it would be circular.  Here
        the detection sits exactly on the hold's propagated prediction while the
        live fix's own prediction is CAL_CLAIM_DELAY_US + 2 µs away (still
        inside the 10 µs claim gate, so the claim stands and the hold is
        refreshed): nothing may be recorded.
        """
        geo = _register()
        pd, pf = _stationary_pred(geo)
        # Frame 1 predicts exactly on the detection, so path 2 claims; from
        # frame 2 the cache's prediction drifts and only the fresh-fix
        # residual can see it.
        offset = [0.0]
        monkeypatch.setattr(
            kc,
            "predict_observation",
            lambda g, lat, lon, alt_km, ve=0.0, vn=0.0, vu=0.0: (pd + offset[0], pf),
        )
        ts0 = int(time.time() * 1000)
        _claim_frames(1, ts0, [pd], [pf])
        offset[0] = CAL_CLAIM_DELAY_US + 2.0
        _claim_frames(CAL_CLAIM_MIN_CLAIMS + 1, ts0 + _FRAME_DT_MS, [pd], [pf])

        assert state.known_hold_claims >= CAL_CLAIM_MIN_CLAIMS, "path H must have made the later claims"
        assert state.known_claims["aaa111"][-1]["fix_refreshed"] is True
        assert _n_points() == 0
        assert state.calibration_claims_rejected_residual >= 1
        assert state.calibration_claims_rejected_hold == 0

    def test_a_refreshed_holds_registry_entry_carries_no_calibration_keys(self, _binding):
        """The calibration position and its reference prediction ride on the
        claim's private `extra` and are filtered back out, so every existing
        reader of state.known_claims sees the record it always saw."""
        geo = _register()
        _dr_lat, _dr_lon, pd, pf = self._moving_fix(geo)
        ts0 = int(time.time() * 1000)

        self._run_blind(CAL_CLAIM_MIN_CLAIMS + 1, ts0, pd, pf)

        rec = state.known_claims["aaa111"][-1]
        assert rec["hold"] is True and rec["fix_refreshed"] is True
        assert set(rec) == {
            "node_id",
            "delay_us",
            "doppler_hz",
            "pred_delay_us",
            "pred_doppler_hz",
            "ts_ms",
            "adsb_fix",
            "contested",
            "hold",
            "hold_gap_s",
            "fix_refreshed",
        }
        assert not [k for k in rec if k in kc._CAL_EXTRA_KEYS]

    def test_hold_claims_record_nothing_once_the_transponder_stops(self, _binding):
        """The case rule 1 is actually written for: no fresh fix at all behind
        the claim, only this node's own prediction of its own measurement.
        Recording it would feed the polygon the polygon's own output."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)

        _claim_frames(1, ts0, [pd], [pf])
        state.adsb_aircraft.clear()
        rejected_hold_before = state.calibration_claims_rejected_hold

        for k in range(1, 4):
            kc.claim_known_targets(_NODE_ID, _frame(ts0 + k * _FRAME_DT_MS, [pd], [pf]))

        assert state.known_hold_claims >= 1, "path H must have run"
        assert _n_points() == 0
        assert state.calibration_claims_rejected_hold >= rejected_hold_before + 3

    def test_a_hold_whose_fix_aged_past_the_claim_cap_records_nothing(self, _binding):
        """The other half of "no live transponder": the entry is still in the
        cache, it has simply stopped being updated.

        Frames every 6 s — inside both KNOWN_HOLD_MAX_GAP_S (8 s) and
        CAL_CLAIM_STREAK_GAP_S (10 s), so the hold and the streak both survive
        — while one unrefreshed fix ages out.  Past CAL_MAX_ADSB_AGE_S the
        refreshed hold is charged to `stale_fix`, and past
        KNOWN_CLAIM_MAX_FIX_AGE_S _fresh_fix_prediction stops answering at all
        and the bare hold is charged to `hold`.  Nothing is ever recorded.
        """
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)
        _cache_state("aaa111", ts0)

        step_ms = 6000
        n = int(kc.KNOWN_CLAIM_MAX_FIX_AGE_S * 1000 / step_ms) + 2
        for k in range(n):
            kc.claim_known_targets(_NODE_ID, _frame(ts0 + k * step_ms, [pd], [pf]))

        assert state.known_hold_claims == n - 1, "the hold must never have expired"
        assert _n_points() == 0
        assert state.calibration_claims_rejected_stale_fix >= 1, "while the fix was merely stale"
        assert state.calibration_claims_rejected_hold >= 1, "once it aged out of the claim gate entirely"

    def test_follow_claims_record_nothing(self, _binding, monkeypatch):
        """A follow candidate IS the lane's own published solve — the one
        provenance record_adsb_calibration's docstring has always refused."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)
        # No cached fix at all, and holds disabled, so the only candidate is
        # the lane's own published entry.
        monkeypatch.setattr(kc, "KNOWN_HOLD_MAX_GAP_S", 0.0)
        state.multinode_tracks["mn-adsb-aaa111"] = {
            "lat": _LAT,
            "lon": _LON,
            "alt_km": _ALT_BARO_FT * FT_TO_M / 1000.0,
            "vel_east": 0.0,
            "vel_north": 0.0,
            "timestamp_ms": ts0,
            "solve_count": 10,
        }

        for k in range(CAL_CLAIM_MIN_CLAIMS + 1):
            kc.claim_known_targets(_NODE_ID, _frame(ts0 + k * _FRAME_DT_MS, [pd], [pf]))

        assert state.known_follow_claims >= 1, "the follow path must have run"
        assert _n_points() == 0
        assert state.calibration_claims_rejected_hold >= 1


class TestNodeTags:
    """Path 1.  A node tag is the node's own correlation, so it is never
    re-gated for CLAIMING — but calibration applies the same five rules to it
    as to anything else."""

    def _tag(self, lat=_LAT, lon=_LON, hexn="aaa111"):
        return {"hex": hexn, "lat": lat, "lon": lon, "alt_baro": _ALT_BARO_FT, "gs": 0, "track": 0}

    def test_a_clean_tag_records(self, _binding):
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)
        for k in range(CAL_CLAIM_MIN_CLAIMS):
            ts = ts0 + k * _FRAME_DT_MS
            kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf], adsb=[self._tag()]))
        assert _n_points() == 1

    def test_a_cached_competitor_inside_the_gate_blocks_a_tag(self, _binding):
        """The exclusivity test reaches the cache even for a tagged detection:
        that is what the widened candidate list is for.  bbb222 sits where the
        tagged aircraft does, so its prediction is inside the claim gate."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)
        for k in range(CAL_CLAIM_MIN_CLAIMS + 1):
            ts = ts0 + k * _FRAME_DT_MS
            _cache_state("bbb222", ts)
            kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf], adsb=[self._tag()]))

        assert state.known_claims_made > 0
        assert _n_points() == 0
        assert state.calibration_claims_rejected_contested >= 1

    def test_a_path1_hex_is_never_assigned_by_path2(self, _binding):
        """Regression for the widened candidate list: the claimed_hexes skip
        moved from candidate construction to the column build, and if it were
        lost the same hex could be claimed twice in one frame."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts = int(time.time() * 1000)
        _cache_state("aaa111", ts)
        # Two detections at the same observation: path 1 takes det 0 on the
        # tag; det 1 is free and aaa111's cached fix explains it perfectly,
        # so only the skip stops path 2 taking it.
        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [pd, pd], [pf, pf], adsb=[self._tag(), None]))

        assert claimed == {0}
        assert len(state.known_claims["aaa111"]) == 1


class TestRecordedPosition:
    def test_the_point_is_the_dead_reckoned_position(self, _binding, _no_holds):
        """Rule 6.  The assignment gates on the fix dead-reckoned to the frame
        instant, so that is the position the point must carry — recording the
        REPORTED fix instead would put the point where the aircraft was
        seconds before the detection, which is the exit-smear bug that
        CAL_FIX_DETECTION_SKEW_S exists to stop on the other path."""
        geo = _register()
        # 400 kt due east, and a fix 8 s old at every frame — ~1.6 km of
        # travel, several 5° bins at this range.
        age_s = 8.0
        gs, track = 400.0, 90.0
        fix_lat, fix_lon = _LAT, _LON
        ve = gs * 0.514444
        dr_lat, dr_lon = offset_latlon_m(fix_lat, fix_lon, east_m=ve * age_s, north_m=0.0)
        pd, pf = predict_observation(geo, dr_lat, dr_lon, _ALT_BARO_FT * FT_TO_M / 1000.0, ve, 0.0)

        ts0 = int(time.time() * 1000)
        for k in range(CAL_CLAIM_MIN_CLAIMS):
            ts = ts0 + k * _FRAME_DT_MS
            _cache_state("aaa111", ts - int(age_s * 1000), gs=gs, track=track)
            kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]))

        ec = state.node_analytics.empirical_coverages[_NODE_ID]
        assert ec.n_points == 1
        dr_bearing, dr_range = _bearing_and_range(geo.rx_lat, geo.rx_lon, dr_lat, dr_lon)
        rep_bearing, rep_range = _bearing_and_range(geo.rx_lat, geo.rx_lon, fix_lat, fix_lon)
        assert _bin_for_bearing(dr_bearing) != _bin_for_bearing(rep_bearing), (
            "the test is only meaningful if the two positions land in different bins"
        )
        recorded = ec._bins[_bin_for_bearing(dr_bearing)]
        assert len(recorded) == 1
        assert recorded[0] == pytest.approx(dr_range, abs=0.05)
        assert ec._bins[_bin_for_bearing(rep_bearing)] == []
