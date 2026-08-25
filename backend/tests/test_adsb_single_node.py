"""Single-node ADS-B display: the `adsb_single_node` feed section.

When exactly one node is claiming a transponder (services/known_claiming.py),
nothing else in the feed renders that aircraft: the claimed detection has left
the dark pool, and the known-lane solver needs n>=2 before it publishes
anything.  `_claimed_single_node_entries` is that aircraft's only publication
path, so these tests pin the qualification rule (one distinct fresh node), the
payload the frontend codes against, and the two collisions the entry can get
into downstream — dedup against a solver estimate for the same aircraft, and
the per-node WS filter.

The registry is populated directly rather than through the claiming stage: the
feed section reads `state.known_claims` and knows nothing about how a record
got there, and going through frame ingest would test the claimer instead.
"""

import os
import time
import types
from collections import deque

import orjson
import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from config.constants import ARC_MIN_DIFFERENTIAL_KM, CLAIMED_DISPLAY_FRESH_S  # noqa: E402
from core import state  # noqa: E402
from services.aircraft_feed import _claimed_single_node_entries  # noqa: E402
from services.feed_helpers import dedup_aircraft  # noqa: E402
from services.geo import C_KM_US  # noqa: E402
from services.tasks.aircraft_flush import filter_payload_to_nodes  # noqa: E402
from tests.probe_helpers import run_probe  # noqa: E402

# Real Atlanta-area bistatic geometry, same as test_arc_builder's: the arc
# assertions below are worthless against a node the builder would decline for
# reasons other than the one under test.
_NODE_CFG = {
    "node_id": "node-a",
    "rx_lat": 33.939182,
    "rx_lon": -84.651910,
    "tx_lat": 33.756670,
    "tx_lon": -84.331844,
    "beam_width_deg": 90,
    "max_range_km": 100,
}

_HEX = "abc123"
_FIX_LAT, _FIX_LON = 33.85, -84.5
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


def _claim(node_id: str = "node-a", age_s: float = 0.0, delay_us: float = _DELAY_US) -> dict:
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
            "gs": 420.0,
            "track": 187.0,
            "fix_ts_ms": ts_ms - 1200,
        },
        "contested": False,
    }


def _seed(*claims: dict, hexn: str = _HEX) -> None:
    state.known_claims[hexn] = deque(claims, maxlen=state.KNOWN_CLAIMS_PER_HEX_MAX)


class TestQualification:
    """Which hexes get an entry at all."""

    def test_one_fresh_claim_emits_the_contract_entry(self):
        _register_node()
        _seed(_claim())
        state.adsb_aircraft[_HEX] = {"hex": _HEX, "flight": "DAL1234 ", "last_seen_ms": int(time.time() * 1000)}

        (entry,) = _claimed_single_node_entries(time.time())

        assert entry["hex"] == _HEX
        assert entry["type"] == "adsb_icao"
        assert entry["position_source"] == "adsb_single_node"
        # The position is the transponder's own fix, not an estimate derived
        # from it — that is the whole claim this source makes.
        assert (entry["lat"], entry["lon"]) == (_FIX_LAT, _FIX_LON)
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

        (entry,) = _claimed_single_node_entries(time.time())

        assert entry["flight"] is None

    def test_two_distinct_nodes_emit_nothing(self):
        """>=2 claiming nodes is the known-lane solver's case (it needs n>=2 and
        publishes mn-adsb-<hex> on convergence).  Emitting here as well would
        draw the same aircraft twice."""
        _register_node("node-a")
        _register_node("node-b")
        _seed(_claim("node-a"), _claim("node-b"))

        assert _claimed_single_node_entries(time.time()) == []

    def test_repeat_claims_from_one_node_still_qualify(self):
        """The gate counts distinct nodes, not claims: one node detecting the
        aircraft every frame is the normal case, not a disqualifying one."""
        _register_node()
        _seed(_claim(age_s=2.0), _claim(age_s=1.0), _claim(age_s=0.0))

        (entry,) = _claimed_single_node_entries(time.time())

        assert entry["node_id"] == "node-a"

    def test_stale_claims_emit_nothing(self):
        _register_node()
        _seed(_claim(age_s=CLAIMED_DISPLAY_FRESH_S + 1.0))

        assert _claimed_single_node_entries(time.time()) == []

    def test_a_stale_second_node_does_not_disqualify(self):
        """Only fresh claims count toward the distinct-node gate — the registry
        holds two minutes of history, so a node that stopped detecting the
        aircraft would otherwise suppress the display for the one that has."""
        _register_node("node-a")
        _register_node("node-b")
        _seed(_claim("node-a"), _claim("node-b", age_s=CLAIMED_DISPLAY_FRESH_S + 1.0))

        (entry,) = _claimed_single_node_entries(time.time())

        assert entry["node_id"] == "node-a"

    def test_newest_fresh_claim_supplies_the_measurement(self):
        _register_node()
        _seed(_claim(age_s=3.0, delay_us=90.0), _claim(age_s=0.5, delay_us=150.0))

        (entry,) = _claimed_single_node_entries(time.time())

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

        (entry,) = _claimed_single_node_entries(time.time())

        assert entry["ambiguity_arc"] is None
        assert entry["position_source"] == "adsb_single_node"
        assert (entry["lat"], entry["lon"]) == (_FIX_LAT, _FIX_LON)

    def test_a_disconnected_node_leaves_the_entry_without_an_arc(self):
        """The claim outlives the node's pipeline by up to the freshness
        window; the ADS-B position is still good, only the geometry is gone."""
        _seed(_claim())

        (entry,) = _claimed_single_node_entries(time.time())

        assert entry["ambiguity_arc"] is None
        assert entry["node_id"] == "node-a"


class TestDownstream:
    def test_the_builder_publishes_the_entry_and_leaves_detection_arcs_alone(self):
        """The arc rides on the aircraft entry.  detection_arcs[] is the
        afterglow-trail channel — an entry there would be faded by the
        frontend's arc buffer instead of living and dying with the claim."""
        from services.aircraft_feed import build_combined_aircraft_json

        _register_node()
        _seed(_claim())

        result = build_combined_aircraft_json(types.SimpleNamespace(geolocated_tracks={}, config={}))

        (entry,) = [ac for ac in result["aircraft"] if ac["position_source"] == "adsb_single_node"]
        assert entry["hex"] == _HEX
        assert entry["ambiguity_arc"] is not None
        assert result["detection_arcs"] == []

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
        entries = _claimed_single_node_entries(time.time())
        payload = {"now": time.time(), "aircraft": entries, "detection_arcs": []}

        kept = orjson.loads(filter_payload_to_nodes(payload, {"node-a"}))
        dropped = orjson.loads(filter_payload_to_nodes(payload, {"node-z"}))

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
