"""adsb-service candidates in claiming: offered beneath a node's own ADS-B, never over it.

The fallback store (state.adsb_fallback, filled by services/tasks/adsb_fallback)
reaches claiming through the seeding snapshot, state._adsb_for_seeding, and
nowhere else: the feed and track enrichment keep reading node-sent ADS-B alone.
"""

import time

import pytest

from config.constants import ADSB_NODE_FIX_PRECEDENCE_S, CAL_CLAIM_MIN_CLAIMS, CAL_MAX_ADSB_AGE_S
from core import state
from services import known_claiming as kc
from services import track_gates
from services.adsb_regions import cell_of
from services.tasks import adsb_fallback
from tests.node_helpers import register_test_node
from tests.test_known_claiming import _ALT_BARO_FT, _LAT, _LON, _NODE_CFG, _frame, _stationary_pred

# Not a test-* id: those are synthetic by prefix, and the fallback is real traffic.
_NODE_ID = "ndefallback0001"
_RX = (_NODE_CFG["rx_lat"], _NODE_CFG["rx_lon"])
_CELL = cell_of(*_RX)
# Another region, a lattice cell away.
_ELSEWHERE = cell_of(_RX[0] + 5.0, _RX[1])


def _register(node_id=_NODE_ID, synthetic=False):
    register_test_node(node_id, dict(_NODE_CFG, node_id=node_id), is_synthetic=synthetic)
    return state.node_associator.node_geometries[node_id]


def _fallback(hexn, ts_ms, lat=_LAT, lon=_LON, gs=0.0, track=0.0, cell=_CELL, flight="FB1"):
    """A record exactly as the poller stores one, in the answer of the region under ``cell``."""
    row = {"hex": hexn, "flight": flight, "lat": lat, "lon": lon, "alt_baro": _ALT_BARO_FT, "gs": gs, "track": track}
    rec = adsb_fallback._as_candidate({**row, "captured_ms": ts_ms})
    state.adsb_fallback = {**state.adsb_fallback, cell: {**state.adsb_fallback.get(cell, {}), hexn: rec}}


def _node_fed(hexn, ts_ms, lat=_LAT, lon=_LON):
    """A record as frame_processor files a node's own tag: world "real", derived fields on."""
    rec = {"hex": hexn, "flight": "NODE1", "lat": lat, "lon": lon, "alt_baro": _ALT_BARO_FT, "gs": 0, "track": 0}
    rec.update(last_seen_ms=ts_ms, recv_ms=ts_ms, world="real")
    rec.update(state.adsb_derived_fields(rec))
    state.adsb_aircraft[hexn] = rec


@pytest.fixture
def _shadow(monkeypatch):
    # The mode staging and production roll the fallback out under.
    monkeypatch.setattr(state, "KNOWN_LANE_MODE", "shadow")


class TestTheSeedingSnapshot:
    def test_a_fallback_record_is_a_candidate(self):
        _fallback("aaa111", 1_000)
        assert state._adsb_for_seeding()["aaa111"]["source"] == "adsb_service"

    def test_a_nodes_own_fix_outranks_a_fallback_fix_inside_the_window(self):
        t = 1_000_000
        _node_fed("aaa111", t)
        _fallback("aaa111", t + int((ADSB_NODE_FIX_PRECEDENCE_S - 1) * 1000), lat=_LAT + 0.01)
        snap = state._adsb_for_seeding()
        assert list(snap) == ["aaa111"]  # one candidate per hex, never two
        assert snap["aaa111"]["flight"] == "NODE1"

    def test_a_fallback_fix_newer_by_more_than_the_window_replaces_a_stale_node_fix(self):
        t = 1_000_000
        _node_fed("aaa111", t)
        _fallback("aaa111", t + int((ADSB_NODE_FIX_PRECEDENCE_S + 1) * 1000))
        assert state._adsb_for_seeding()["aaa111"]["flight"] == "FB1"

    def test_precedence_is_judged_on_our_clock_not_the_nodes(self):
        # A node clock 12 s slow stamps a live tag 12 s back; its receipt is ours.
        t = 1_000_000
        _node_fed("aaa111", t - 12_000)
        state.adsb_aircraft["aaa111"]["recv_ms"] = t
        _fallback("aaa111", t)
        assert state._adsb_for_seeding()["aaa111"]["flight"] == "NODE1"

    def test_an_unusable_node_record_leaves_the_fallback_standing(self):
        _node_fed("aaa111", 1_000_000)
        state.adsb_aircraft["aaa111"]["lat"] = None
        _fallback("aaa111", 1_000_000)
        assert state._adsb_for_seeding()["aaa111"]["flight"] == "FB1"

    def test_a_node_is_offered_its_own_regions_answer_alone(self):
        _fallback("aaa111", 1_000)
        _fallback("bbb222", 1_000, cell=_ELSEWHERE)
        assert set(state._adsb_for_seeding(_RX)) == {"aaa111"}

    def test_without_a_node_every_region_is_offered_the_newest_fix_winning(self):
        _fallback("aaa111", 1_000, flight="OLD")
        _fallback("aaa111", 2_000, cell=_ELSEWHERE, flight="NEW")
        _fallback("bbb222", 1_000, cell=_ELSEWHERE)
        snap = state._adsb_for_seeding()
        assert set(snap) == {"aaa111", "bbb222"}
        assert snap["aaa111"]["flight"] == "NEW"

    def test_an_unplaceable_node_is_offered_no_polled_candidate(self):
        _fallback("aaa111", 1_000)
        assert state._adsb_for_seeding((float("nan"), 0.0)) == {}

    def test_the_absent_position_sentinel_is_no_place(self):
        # (0, 0) is how an omitted position arrives, and it has a cell of its own.
        _fallback("aaa111", 1_000, cell=cell_of(0.0, 0.0))
        assert state._adsb_for_seeding((0.0, 0.0)) == {}

    def test_writing_into_the_snapshot_touches_neither_store(self):
        _fallback("aaa111", 1_000)
        _node_fed("bbb222", 1_000)
        for snap in (state._adsb_for_seeding(), state._adsb_for_seeding(_RX)):
            snap["ccc333"] = {}
            del snap["aaa111"]
        assert set(state.adsb_fallback[_CELL]) == {"aaa111"}
        assert set(state.adsb_aircraft) == {"bbb222"}

    def test_track_enrichment_never_sees_a_fallback_fix(self):
        now = time.time()
        _fallback("aaa111", int(now * 1000))
        assert track_gates.fresh_adsb("aaa111", now) is None


class TestClaimingAgainstTheFallback:
    def test_a_real_node_with_no_adsb_of_its_own_claims_against_it(self):
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts = int(time.time() * 1000)
        _fallback("aaa111", ts - 2_000)
        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf])) == {0}
        assert state.known_claims["aaa111"][-1]["adsb_fix"]["fix_ts_ms"] == ts - 2_000

    def test_a_synthetic_node_never_does(self):
        node_id = "synth-fallback-01"
        geo = _register(node_id, synthetic=True)
        pd, pf = _stationary_pred(geo)
        ts = int(time.time() * 1000)
        _fallback("aaa111", ts - 2_000)
        assert kc.claim_known_targets(node_id, _frame(ts, [pd], [pf])) == set()
        assert state.known_claims_world_rejects >= 1

    def test_a_nodes_own_v1_tag_is_claimed_in_preference_to_the_fallback(self):
        """Contract 1.5.0's positioned tag, through the same conversion the v1
        route uses, against a fresher fallback fix for the same aircraft at a
        different position: the claim carries the node's tag."""
        from routes.node_schemas import DetectionFrame
        from services.node_pipeline import pipeline_frame

        geo = _register()
        pd, pf = _stationary_pred(geo)
        now = time.time()
        _fallback("4ca1f2", int(now * 1000), lat=_LAT + 0.01)
        frame = pipeline_frame(
            DetectionFrame(
                t=now,
                seq=1,
                boot_id="k3n8v2qp71ab",
                config_version=1,
                delay=[pd],
                doppler=[pf],
                snr=[20.0],
                adsb_hex=["4ca1f2"],
                adsb=[{"hex": "4ca1f2", "lat": _LAT, "lon": _LON, "alt": _ALT_BARO_FT, "gs": 0, "track": 0}],
            )
        )

        assert kc.claim_known_targets(_NODE_ID, frame) == {0}
        (claim,) = state.known_claims["4ca1f2"]
        assert (claim["adsb_fix"]["lat"], claim["adsb_fix"]["lon"]) == (_LAT, _LON)

    def test_another_node_claims_against_that_tag_rather_than_the_fallback(self):
        """Once a node's tag is filed, a second node with no ADS-B of its own
        binds to the tag's fix, not to a fallback fix a few seconds fresher."""
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts = int(time.time() * 1000)
        _node_fed("aaa111", ts - 3_000)
        _fallback("aaa111", ts - 1_000, lat=_LAT + 0.001)
        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf])) == {0}
        assert state.known_claims["aaa111"][-1]["adsb_fix"]["fix_ts_ms"] == ts - 3_000

    @pytest.mark.parametrize(
        ("fix_age_s", "calibrates"),
        [(CAL_MAX_ADSB_AGE_S - 3, True), (CAL_MAX_ADSB_AGE_S + 5, False)],
    )
    def test_a_fresh_fallback_fix_calibrates_and_a_stale_one_only_claims(self, _shadow, fix_age_s, calibrates):
        geo = _register()
        pd, pf = _stationary_pred(geo)
        ts0 = int(time.time() * 1000)
        n = CAL_CLAIM_MIN_CLAIMS + 2
        for k in range(n):
            ts = ts0 + k * 1000
            _fallback("aaa111", ts - int(fix_age_s * 1000))
            kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]))

        assert state.known_claims_made == n
        ec = state.node_analytics.empirical_coverages.get(_NODE_ID)
        n_points = ec.n_points if ec is not None else 0
        if calibrates:
            assert n_points == n - (CAL_CLAIM_MIN_CLAIMS - 1)
        else:
            assert n_points == 0
            assert state.calibration_claims_rejected_stale_fix > 0
