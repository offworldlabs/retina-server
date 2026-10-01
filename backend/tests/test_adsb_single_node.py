"""Claimed ADS-B display: the `adsb_single_node` / `adsb_multi_node` feed section.

When exactly one node is claiming a transponder (services/known_claiming.py),
nothing else in the feed renders that aircraft: the claimed detection has left
the dark pool, and the known-lane solver needs n>=2 before it publishes
anything.  `_claimed_adsb_entries` is that aircraft's only publication
path, so these tests pin the qualification rule (one distinct fresh node), the
payload the frontend codes against, and the two collisions the entry can get
into downstream — dedup against a solver estimate for the same aircraft, and
the per-node WS filter.

The registry is populated directly rather than through the claiming stage: the
feed section reads `state.known_claims` and knows nothing about how a record
got there, and going through frame ingest would test the claimer instead.
"""

import math
import os
import time
import types
from collections import deque

import pytest

from config.constants import ARC_MIN_DIFFERENTIAL_KM, CLAIMED_DISPLAY_FRESH_S, CLAIMED_DISPLAY_MAX_FIX_AGE_S
from core import state
from services.aircraft_feed import _claimed_adsb_entries
from services.feed_helpers import dedup_aircraft
from services.geo import C_KM_US
from services.tasks.aircraft_flush import filter_payload_to_nodes
from tests.probe_helpers import run_probe

# An invented bistatic geometry, translated as one piece from test_arc_builder's:
# the arc assertions below are worthless against a node the builder would decline
# for reasons other than the one under test, so the shape has to survive even
# though the position is not a real site.
_NODE_CFG = {
    "node_id": "node-a",
    "rx_lat": 34.0,
    "rx_lon": -84.0,
    "tx_lat": 33.82,
    "tx_lon": -83.68,
    "beam_width_deg": 90,
    "max_range_km": 100,
}

_HEX = "abc123"
_FIX_LAT, _FIX_LON = 33.91, -83.85
# Comfortably above ARC_MIN_DIFFERENTIAL_KM once multiplied by C_KM_US.
_DELAY_US = 120.0


def _register_node(node_id: str = "node-a") -> None:
    """Put a pipeline carrying `_NODE_CFG` under `node_id` — the arc geometry
    is looked up through state.node_pipelines, not carried on the claim."""
    cfg = _NODE_CFG | {"node_id": node_id}
    # `tracker` so build_combined_aircraft_json's pending-arc sweep can walk
    # this pipeline too; it finds no tracks, which is the point — a claimed
    # detection reaches the map through the claims registry, not the tracker.
    state.node_pipelines[node_id] = types.SimpleNamespace(
        config=cfg,
        tracker=types.SimpleNamespace(tracks=[]),
    )


def _claim(
    node_id: str = "node-a",
    age_s: float = 0.0,
    delay_us: float = _DELAY_US,
    fix_lag_s: float = 1.2,
    gs: float = 420.0,
    **extra,
) -> dict:
    """One path-2 claim made `age_s` ago on a fix `fix_lag_s` older than it."""
    ts_ms = int((time.time() - age_s) * 1000)
    return {
        "node_id": node_id,
        "delay_us": delay_us,
        "doppler_hz": -30.0,
        "pred_delay_us": delay_us - 1.0,
        "pred_doppler_hz": -29.0,
        "ts_ms": ts_ms,
        "adsb_fix": {
            "lat": _FIX_LAT,
            "lon": _FIX_LON,
            "alt_baro": 34000,
            "gs": gs,
            "track": 187.0,
            "fix_ts_ms": ts_ms - int(fix_lag_s * 1000),
        },
        "contested": False,
        **extra,
    }


def _seed(*claims: dict, hexn: str = _HEX) -> None:
    state.known_claims[hexn] = deque(claims, maxlen=state.KNOWN_CLAIMS_PER_HEX_MAX)


def _dr(lat: float, lon: float, gs_kt: float, track_deg: float, dt_s: float, abs_deg: float = 2e-4) -> tuple:
    """The fix moved `dt_s` along gs/track, rounded as the feed rounds it.

    Written out from the geometry rather than through the feed's own helper
    chain, so a unit slip there (knots vs m/s, a swapped sin/cos) fails here.
    """
    v = gs_kt * 0.514444
    east_m = v * math.sin(math.radians(track_deg)) * dt_s
    north_m = v * math.cos(math.radians(track_deg)) * dt_s
    d_lat = north_m / 111_195.0
    d_lon = east_m / (111_195.0 * math.cos(math.radians(lat)))
    return pytest.approx(lat + d_lat, abs=abs_deg), pytest.approx(lon + d_lon, abs=abs_deg)


class TestQualification:
    """Which hexes get an entry at all."""

    def test_one_fresh_claim_emits_the_contract_entry(self):
        _register_node()
        _seed(_claim())
        state.adsb_aircraft[_HEX] = {"hex": _HEX, "flight": "DAL1234 ", "last_seen_ms": int(time.time() * 1000)}

        (entry,) = _claimed_adsb_entries(time.time())

        assert entry["hex"] == _HEX
        assert entry["type"] == "adsb_icao"
        assert entry["position_source"] == "adsb_single_node"
        # The position is the transponder's own fix, dead-reckoned the 1.2 s
        # it has aged along its own gs/track — not an estimate derived from
        # the radar, which is the whole claim this source makes.
        assert (entry["lat"], entry["lon"]) == _dr(_FIX_LAT, _FIX_LON, 420.0, 187.0, entry["adsb_fix_age_s"])
        assert entry["alt_baro"] == 34000
        assert (entry["gs"], entry["track"]) == (420.0, 187.0)
        assert entry["node_id"] == "node-a"
        assert entry["multinode"] is False
        assert entry["target_class"] == "aircraft"
        assert entry["delay_us"] == pytest.approx(_DELAY_US)
        assert entry["doppler_hz"] == pytest.approx(-30.0)
        assert entry["seen"] < 1.0
        assert entry["adsb_fix_age_s"] == pytest.approx(1.2, abs=0.2)
        # Callsign comes from the ADS-B cache: a claim's fix carries position
        # and kinematics only.
        assert entry["flight"] == "DAL1234"
        # The FULL locus, untrimmed — 37 points for this node's 90 deg wedge.
        assert len(entry["ambiguity_arc"]) == 37

    def test_flight_is_null_without_an_adsb_cache_entry(self):
        _register_node()
        _seed(_claim())

        (entry,) = _claimed_adsb_entries(time.time())

        assert entry["flight"] is None

    def test_two_distinct_nodes_with_a_published_solve_emit_nothing(self):
        """>=2 claiming nodes is the known-lane solver's case (it needs n>=2 and
        publishes mn-adsb-<hex> on convergence).  While that solve is on the
        map, emitting here as well would draw the same aircraft twice."""
        _register_node("node-a")
        _register_node("node-b")
        _seed(_claim("node-a"), _claim("node-b"))

        assert _claimed_adsb_entries(time.time(), {_HEX}) == []

    def test_repeat_claims_from_one_node_still_qualify(self):
        """The gate counts distinct nodes, not claims: one node detecting the
        aircraft every frame is the normal case, not a disqualifying one."""
        _register_node()
        _seed(_claim(age_s=2.0), _claim(age_s=1.0), _claim(age_s=0.0))

        (entry,) = _claimed_adsb_entries(time.time())

        assert entry["node_id"] == "node-a"

    def test_stale_claims_emit_nothing(self):
        _register_node()
        _seed(_claim(age_s=CLAIMED_DISPLAY_FRESH_S + 1.0))

        assert _claimed_adsb_entries(time.time()) == []

    def test_a_stale_second_node_does_not_disqualify(self):
        """Only fresh claims count toward the distinct-node gate — the registry
        holds two minutes of history, so a node that stopped detecting the
        aircraft would otherwise suppress the display for the one that has."""
        _register_node("node-a")
        _register_node("node-b")
        _seed(_claim("node-a"), _claim("node-b", age_s=CLAIMED_DISPLAY_FRESH_S + 1.0))

        (entry,) = _claimed_adsb_entries(time.time())

        assert entry["node_id"] == "node-a"

    def test_newest_fresh_claim_supplies_the_measurement(self):
        _register_node()
        _seed(_claim(age_s=3.0, delay_us=90.0), _claim(age_s=0.5, delay_us=150.0))

        (entry,) = _claimed_adsb_entries(time.time())

        assert entry["delay_us"] == pytest.approx(150.0)


class TestArc:
    """`ambiguity_arc` is null wherever the geometry is unavailable, never a
    stub the frontend would have to recognise."""

    def test_below_the_differential_floor_the_entry_survives_without_an_arc(self):
        _register_node()
        # A differential well under ARC_MIN_DIFFERENTIAL_KM: the builder
        # declines rather than emit a sliver hugging the TX-RX baseline.
        tiny_delay_us = (ARC_MIN_DIFFERENTIAL_KM / C_KM_US) / 10.0
        _seed(_claim(delay_us=tiny_delay_us))

        (entry,) = _claimed_adsb_entries(time.time())

        assert entry["ambiguity_arc"] is None
        assert entry["position_source"] == "adsb_single_node"
        assert (entry["lat"], entry["lon"]) == _dr(_FIX_LAT, _FIX_LON, 420.0, 187.0, entry["adsb_fix_age_s"])

    def test_a_disconnected_node_leaves_the_entry_without_an_arc(self):
        """The claim outlives the node's pipeline by up to the freshness
        window; the ADS-B position is still good, only the geometry is gone."""
        _seed(_claim())

        (entry,) = _claimed_adsb_entries(time.time())

        assert entry["ambiguity_arc"] is None
        assert entry["node_id"] == "node-a"


class TestFixAge:
    """The icon is the transponder's fix moved to `now`, and only while that
    fix is young enough to move honestly (CLAIMED_DISPLAY_MAX_FIX_AGE_S).

    The a271b0 episode (2026-09-29): a fresh claim can carry a fix up to
    KNOWN_CLAIM_MAX_FIX_AGE_S (45 s) old, so gating on the claim's age alone
    drew a fix ~49 s stale, unmoving, with adsb_stale=false.
    """

    def test_the_fix_is_dead_reckoned_to_now(self):
        _register_node()
        c = _claim(fix_lag_s=4.0)
        _seed(c)
        now = c["ts_ms"] / 1000.0 + 1.0

        (entry,) = _claimed_adsb_entries(now)

        # 5 s at 420 kt ≈ 1.08 km, mostly south: far outside rounding, so a
        # frozen icon cannot pass this.
        assert (entry["lat"], entry["lon"]) == _dr(_FIX_LAT, _FIX_LON, 420.0, 187.0, 5.0, abs_deg=2e-5)
        assert entry["lat"] < _FIX_LAT - 0.009
        assert entry["adsb_fix_age_s"] == pytest.approx(5.0)
        # Speed and heading are the fix's own, not re-derived.
        assert (entry["gs"], entry["track"]) == (420.0, 187.0)

    def test_a_stationary_fix_stays_put(self):
        _register_node()
        _seed(_claim(gs=0.0, fix_lag_s=3.0))

        (entry,) = _claimed_adsb_entries(time.time())

        assert (entry["lat"], entry["lon"]) == (_FIX_LAT, _FIX_LON)

    def test_a_fresh_claim_on_a_stale_fix_is_not_drawn(self):
        """The live shape: path 2 bound a 45 s-old cached fix a second ago."""
        _register_node()
        _seed(_claim(age_s=1.0, fix_lag_s=45.0))

        assert _claimed_adsb_entries(time.time()) == []

    def test_the_gate_is_measured_at_now_not_at_the_claim(self):
        """A fix 8 s old at claim time is inside the bound then, and 3 s later
        it is not: what the map would draw is the fix at `now`."""
        _register_node()
        c = _claim(fix_lag_s=CLAIMED_DISPLAY_MAX_FIX_AGE_S - 2.0)
        _seed(c)
        claim_s = c["ts_ms"] / 1000.0

        assert len(_claimed_adsb_entries(claim_s + 1.0)) == 1
        assert _claimed_adsb_entries(claim_s + 3.0) == []

    def test_an_undated_fix_is_not_drawn(self):
        _register_node()
        c = _claim()
        c["adsb_fix"]["fix_ts_ms"] = 0
        _seed(c)

        assert _claimed_adsb_entries(time.time()) == []

    def test_a_hold_inside_the_bound_is_drawn_and_flagged(self):
        """A hold keeps the radar link but not the transponder: drawn while
        its fix is young, marked adsb_stale, and gone past the bound (the old
        cap here was the claiming path's own 45 s)."""
        _register_node()
        _seed(_claim(fix_lag_s=3.0, hold=True))
        (entry,) = _claimed_adsb_entries(time.time())
        assert entry["adsb_stale"] is True

        _seed(_claim(fix_lag_s=20.0, hold=True))
        assert _claimed_adsb_entries(time.time()) == []

    def test_a_path2_claim_is_not_flagged_stale(self):
        _register_node()
        _seed(_claim(fix_lag_s=3.0))

        (entry,) = _claimed_adsb_entries(time.time())

        assert entry["adsb_stale"] is False


class TestMultiNode:
    """>=2 fresh claiming nodes and no known-lane solve on the map: the
    aircraft still gets an icon (a271b0: two real nodes, every n=2 solve
    rejected, drawn nowhere)."""

    def _two(self, **kw):
        _register_node("node-a")
        _register_node("node-b")
        _seed(_claim("node-a", **kw), _claim("node-b", **kw))

    def test_emits_the_multi_entry_when_no_solve_is_published(self):
        self._two()
        state.adsb_aircraft[_HEX] = {"hex": _HEX, "flight": "MXY1545", "last_seen_ms": int(time.time() * 1000)}

        (entry,) = _claimed_adsb_entries(time.time(), set())

        assert entry["hex"] == _HEX
        assert entry["position_source"] == "adsb_multi_node"
        assert entry["n_nodes"] == 2
        assert entry["contributing_node_ids"] == ["node-a", "node-b"]
        assert entry["multinode"] is False
        assert entry["flight"] == "MXY1545"
        assert (entry["lat"], entry["lon"]) == _dr(_FIX_LAT, _FIX_LON, 420.0, 187.0, entry["adsb_fix_age_s"])
        # No one node's measurement or locus: the position is nobody's arc.
        for key in ("node_id", "ambiguity_arc", "delay_us", "doppler_hz"):
            assert key not in entry

    def test_a_published_solve_for_another_hex_does_not_suppress_it(self):
        self._two()

        (entry,) = _claimed_adsb_entries(time.time(), {"ffffff"})

        assert entry["position_source"] == "adsb_multi_node"

    def test_the_fix_age_gate_applies_too(self):
        self._two(fix_lag_s=CLAIMED_DISPLAY_MAX_FIX_AGE_S + 5.0)

        assert _claimed_adsb_entries(time.time(), set()) == []

    def test_the_freshest_fix_anchors_the_entry(self):
        """A hold claim carries its original, older fix; a neighbour's path-2
        claim the same second carries a newer one.  One transponder, so the
        newer fix is the better position."""
        _register_node("node-a")
        _register_node("node-b")
        _seed(_claim("node-a", fix_lag_s=8.0, hold=True), _claim("node-b", fix_lag_s=1.0))

        (entry,) = _claimed_adsb_entries(time.time(), set())

        assert entry["adsb_fix_age_s"] == pytest.approx(1.0, abs=0.3)
        assert entry["adsb_stale"] is False

    def test_the_count_is_distinct_fresh_nodes(self):
        _register_node("node-a")
        _register_node("node-b")
        _register_node("node-c")
        _seed(
            _claim("node-a", age_s=1.0),
            _claim("node-a"),
            _claim("node-b"),
            _claim("node-c", age_s=CLAIMED_DISPLAY_FRESH_S + 1.0),
        )

        (entry,) = _claimed_adsb_entries(time.time(), set())

        assert entry["n_nodes"] == 2
        assert entry["contributing_node_ids"] == ["node-a", "node-b"]

    def test_the_builder_never_draws_it_beside_the_known_lane_solve(self, monkeypatch):
        """End to end through build_combined_aircraft_json: with the hex's
        mn-adsb entry on the map the claimed entry is withheld; with that entry
        gated off the map (an unconfirmed n=2 solve) it is drawn."""
        from services.aircraft_feed import build_combined_aircraft_json

        monkeypatch.setattr(state, "KNOWN_LANE_MODE", "binding")
        self._two()
        now_ms = int(time.time() * 1000)
        solve = {
            "lat": _FIX_LAT,
            "lon": _FIX_LON,
            "alt_m": 10_363.0,
            "vel_east": 0.0,
            "vel_north": -200.0,
            "timestamp_ms": now_ms,
            "n_nodes": 2,
            "n_measurements": 4,
            "rms_delay": 0.5,
            "rms_doppler": 2.0,
            "contributing_node_ids": ["node-a", "node-b"],
            "solve_count": 3,
        }
        state.multinode_tracks[f"mn-adsb-{_HEX}"] = dict(solve)
        pipeline = types.SimpleNamespace(geolocated_tracks={}, config={})

        result = build_combined_aircraft_json(pipeline)
        sources = [ac["position_source"] for ac in result["aircraft"]]
        assert sources == ["multinode_solve"]
        assert result["aircraft"][0]["adsb_hex"] == _HEX

        # An n=2 solve seen once is held back by the display gate
        # (MN_N2_MIN_SOLVES): no solve on the map, so the ADS-B entry is.
        state.multinode_tracks[f"mn-adsb-{_HEX}"] = dict(solve, solve_count=1)
        result = build_combined_aircraft_json(pipeline)
        assert [ac["position_source"] for ac in result["aircraft"]] == ["adsb_multi_node"]

    def test_it_reaches_each_claiming_nodes_ws_feed(self):
        self._two()
        payload = {"now": time.time(), "aircraft": _claimed_adsb_entries(time.time(), set()), "detection_arcs": []}

        assert [ac["hex"] for ac in filter_payload_to_nodes(payload, {"node-b"})["aircraft"]] == [_HEX]
        assert filter_payload_to_nodes(payload, {"node-z"})["aircraft"] == []

    def test_dedup_ranks_it_with_the_single_node_source(self):
        multi = {"hex": _HEX, "lat": _FIX_LAT, "lon": _FIX_LON, "position_source": "adsb_multi_node"}
        arc = {"hex": "pr1", "lat": _FIX_LAT, "lon": _FIX_LON, "position_source": "single_node_ellipse_arc"}

        (winner,) = dedup_aircraft([arc, multi])

        assert winner["position_source"] == "adsb_multi_node"


class TestClaimHistory:
    """state.known_claim_history: every claim once, with the feed's decision,
    surviving the 120 s known_claims prune."""

    def test_each_claim_is_recorded_once_with_the_feed_decision(self):
        _register_node()
        c = _claim(fix_lag_s=2.0)
        _seed(c)
        now = time.time()

        _claimed_adsb_entries(now)
        _claimed_adsb_entries(now + 0.5)

        (rec,) = state.known_claim_history
        assert rec["hex"] == _HEX
        assert rec["node_id"] == "node-a"
        assert rec["ts_ms"] == c["ts_ms"]
        assert rec["feed"] == "adsb_single_node"
        assert rec["n_fresh_nodes"] == 1
        # measured - predicted, as the trust path signs it.
        assert rec["residual_delay_us"] == pytest.approx(1.0)
        assert rec["residual_doppler_hz"] == pytest.approx(-1.0)
        assert rec["fix_age_s"] == pytest.approx(2.0)
        assert rec["hold"] is False
        # The raw measurement stays out: beside a known fix it is a ranging
        # circle around the true receiver.
        for key in ("delay_us", "doppler_hz", "pred_delay_us", "pred_doppler_hz"):
            assert key not in rec

    def test_the_a271b0_shape_is_replayable(self):
        """Two nodes, no solve on the map, then both go on claiming against a
        fix that has aged past the bound: the history says what the map did
        at each step, per claim."""
        _register_node("node-a")
        _register_node("node-b")
        _seed(_claim("node-a", age_s=0.5), _claim("node-b", age_s=0.5))
        _claimed_adsb_entries(time.time(), set())

        state.known_claims[_HEX].extend([_claim("node-a", fix_lag_s=40.0), _claim("node-b", fix_lag_s=40.0)])
        _claimed_adsb_entries(time.time(), set())

        rows = [(r["node_id"], r["feed"], r["n_fresh_nodes"]) for r in state.known_claim_history]
        assert rows == [
            ("node-a", "adsb_multi_node", 2),
            ("node-b", "adsb_multi_node", 2),
            ("node-a", "stale_fix", 2),
            ("node-b", "stale_fix", 2),
        ]
        assert [r["fix_age_s"] for r in state.known_claim_history][2:] == [40.0, 40.0]

    def test_a_published_solve_and_a_stale_fix_are_named(self):
        _register_node("node-a")
        _register_node("node-b")
        _seed(_claim("node-a"), _claim("node-b"))
        _claimed_adsb_entries(time.time(), {_HEX})
        _seed(_claim("node-a", fix_lag_s=30.0), hexn="bbb222")
        _claimed_adsb_entries(time.time(), set())

        by_hex = {r["hex"]: r for r in state.known_claim_history}
        assert by_hex[_HEX]["feed"] == "solve_published"
        assert by_hex["bbb222"]["feed"] == "stale_fix"
        assert by_hex["bbb222"]["feed_fix_age_s"] == pytest.approx(30.0, abs=0.5)

    def test_outside_binding_it_still_records(self):
        _register_node()
        _seed(_claim())

        assert _claimed_adsb_entries(time.time(), draw=False) == []

        (rec,) = state.known_claim_history
        assert rec["feed"] == "not_binding"

    def test_it_outlives_the_registry_prune(self):
        _register_node()
        _seed(_claim())
        _claimed_adsb_entries(time.time())
        state.known_claims.clear()
        _claimed_adsb_entries(time.time())

        assert len(state.known_claim_history) == 1

    def test_old_records_age_out(self, monkeypatch):
        monkeypatch.setattr(state, "KNOWN_CLAIM_HISTORY_WINDOW_S", 10.0)
        _register_node()
        _seed(_claim())
        now = time.time()
        _claimed_adsb_entries(now)
        _seed(_claim("node-a"), hexn="bbb222")
        _claimed_adsb_entries(now + 11.0)

        assert [r["hex"] for r in state.known_claim_history] == ["bbb222"]

    def test_memory_is_capped(self):
        assert state.known_claim_history.maxlen == state.KNOWN_CLAIM_HISTORY_MAX

    def test_dark_follow_claims_are_not_recorded_or_drawn(self):
        _seed(
            {"node_id": "node-a", "ts_ms": int(time.time() * 1000), "dark_follow": True, "delay_us": 1.0},
            hexn="mn-dark-abc",
        )

        assert _claimed_adsb_entries(time.time()) == []
        assert not state.known_claim_history


class TestClaimHistoryEndpoint:
    """GET /api/test/mlat-history?kind=claims — the filters.  The identity
    boundary it shares with the other kinds is pinned in test_publication.py
    (TestMlatHistoryPayload), so the republish pass is stubbed here to keep
    these tests about selection."""

    @pytest.fixture(autouse=True)
    def _no_identity_pass(self, monkeypatch):
        import routes.test as test_routes

        monkeypatch.setattr(test_routes, "_published_records", list)

    def _get(self, query: str) -> dict:
        from fastapi.testclient import TestClient

        from main import app

        return TestClient(app).get(f"/api/test/mlat-history?kind=claims{query}").json()

    def test_dumps_and_filters_by_hex(self):
        _register_node()
        _seed(_claim())
        _seed(_claim(), hexn="bbb222")
        _claimed_adsb_entries(time.time())

        data = self._get("")
        assert data["kind"] == "claims"
        assert data["n_records"] == 2
        assert data["by_feed"] == {"adsb_single_node": 2}
        assert data["window_effective_minutes"] >= 0.0

        one = self._get("&hex=BBB222")
        assert one["hex"] == "bbb222"
        assert [r["hex"] for r in one["records"]] == ["bbb222"]

    def test_minutes_window_applies(self):
        _seed(_claim(age_s=120.0))
        _claimed_adsb_entries(time.time())

        assert self._get("&minutes=1")["n_records"] == 0
        assert self._get("&minutes=5")["n_records"] == 1

    def test_limit_caps_the_records_not_the_count(self):
        _register_node()
        _seed(*(_claim(age_s=i) for i in range(5)))
        _claimed_adsb_entries(time.time())

        data = self._get("&limit=2")
        assert data["n_records"] == 5
        assert len(data["records"]) == 2
        # Newest first.
        assert data["records"][0]["ts_ms"] > data["records"][1]["ts_ms"]


class TestDownstream:
    def test_the_builder_publishes_the_entry_and_leaves_detection_arcs_alone(self, monkeypatch):
        """The arc rides on the aircraft entry.  detection_arcs[] is the
        afterglow-trail channel — an entry there would be faded by the
        frontend's arc buffer instead of living and dying with the claim."""
        from services.aircraft_feed import build_combined_aircraft_json

        monkeypatch.setattr(state, "KNOWN_LANE_MODE", "binding")
        _register_node()
        _seed(_claim())

        result = build_combined_aircraft_json(types.SimpleNamespace(geolocated_tracks={}, config={}))

        (entry,) = [ac for ac in result["aircraft"] if ac["position_source"] == "adsb_single_node"]
        assert entry["hex"] == _HEX
        assert entry["ambiguity_arc"] is not None
        assert result["detection_arcs"] == []

    @pytest.mark.parametrize("mode", ["shadow", "off"])
    def test_outside_binding_the_builder_publishes_no_claimed_entry(self, monkeypatch, mode):
        """Shadow claims and measures but must leave the live feed as it was:
        the claimed detection is still in the dark pool, so drawing its fix as
        well puts on the map what binding alone is meant to."""
        from services.aircraft_feed import build_combined_aircraft_json

        monkeypatch.setattr(state, "KNOWN_LANE_MODE", mode)
        _register_node()
        _seed(_claim())

        result = build_combined_aircraft_json(types.SimpleNamespace(geolocated_tracks={}, config={}))

        assert [ac for ac in result["aircraft"] if ac["position_source"] == "adsb_single_node"] == []

    def test_dedup_prefers_the_adsb_fix_over_a_solver_estimate(self):
        """In binding mode a partially-claimed aircraft can still carry a
        tracker track keyed by its ADS-B hex, so this collision is real.  The
        ADS-B fix wins: it IS the position, where the solver entry only
        estimates it from one node's geometry."""
        claimed = {
            "hex": _HEX,
            "lat": _FIX_LAT,
            "lon": _FIX_LON,
            "alt_baro": 34000,
            "position_source": "adsb_single_node",
            "node_id": "node-a",
        }
        solved = {
            "hex": _HEX,
            "lat": _FIX_LAT + 0.01,
            "lon": _FIX_LON + 0.01,
            "alt_baro": 34000,
            "position_source": "solver_adsb_seed",
            "node_id": "node-b",
        }

        # Both orders: dedup keeps the first entry of a group as the group's
        # representative, so an order-dependent rank would pass one and fail
        # the other.
        for pair in ((claimed, solved), (solved, claimed)):
            (winner,) = dedup_aircraft([dict(p) for p in pair])
            assert winner["position_source"] == "adsb_single_node"
            # Both nodes ride along on the survivor, or the losing node's own
            # WS feed stops showing the aircraft it is detecting.
            assert set(winner["contributing_node_ids"]) == {"node-a", "node-b"}

    def test_multinode_still_outranks_it(self):
        claimed = {"hex": _HEX, "lat": _FIX_LAT, "lon": _FIX_LON, "position_source": "adsb_single_node"}
        mn = {"hex": "mn-x", "lat": _FIX_LAT, "lon": _FIX_LON, "position_source": "multinode_solve"}

        (winner,) = dedup_aircraft([claimed, mn])

        assert winner["position_source"] == "multinode_solve"

    def test_entry_survives_the_per_node_ws_filter(self):
        """node_id is mandatory on the contract because of exactly this: the
        live and owner feeds drop every entry whose node_id is not theirs."""
        _register_node()
        _seed(_claim())
        entries = _claimed_adsb_entries(time.time())
        payload = {"now": time.time(), "aircraft": entries, "detection_arcs": []}

        kept = filter_payload_to_nodes(payload, {"node-a"})
        dropped = filter_payload_to_nodes(payload, {"node-z"})

        assert [ac["hex"] for ac in kept["aircraft"]] == [_HEX]
        assert dropped["aircraft"] == []


_MODE_PROBE = """
import json

from core import state

print("PROBE:" + json.dumps({"mode": state.KNOWN_LANE_MODE}))
"""


class TestBindingDefault:
    """KNOWN_LANE_MODE is derived once, as core.state imports, and conftest.py
    pins it to "off" before the first import — so the default can only be
    observed from an interpreter of its own (see tests/probe_helpers.py)."""

    def _probe(self, value: str | None) -> str:
        env = os.environ | {"RETINA_ENV": "test"}
        env.pop("KNOWN_LANE_MODE", None)
        if value is not None:
            env["KNOWN_LANE_MODE"] = value
        return run_probe(_MODE_PROBE, env)["mode"]

    def test_unset_means_binding(self):
        assert self._probe(None) == "binding"

    def test_an_unrecognised_value_degrades_to_shadow(self):
        """Not to the default: a typo should land in the inert mode rather than
        silently arming the acting one."""
        assert self._probe("bidning") == "shadow"
