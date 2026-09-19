"""Real-feed clocks, provenance, units, world isolation and v1 identity plumbing."""

import json
import time

import httpx
import pytest

from core import state
from services.adsb_regions import regions_for_nodes
from services.adsb_truth import node_reference, readsb_references, seeding_references
from services.known_claiming import claim_known_targets, strip_claimed_detections
from services.tasks.adsb_service import fetch_region
from tests.test_known_claiming import _NODE_CFG, _stationary_pred


def envelope(**changes):
    row = {
        "hex": "ABC123",
        "lat": 34.88,
        "lon": -82.35,
        "alt_baro": 23000,
        "gs": 200,
        "track": 90,
        "seen_pos": 2,
        **changes,
    }
    return {"now": 1000, "ac": [row]}


def test_capture_age_is_not_refreshed_on_repoll():
    a = readsb_references(envelope(), 1001)["abc123"]
    b = readsb_references(envelope(), 1020)["abc123"]
    assert a["timestamp_ms"] == b["timestamp_ms"] == 998000
    assert b["alt_m"] == pytest.approx(7010.4)
    assert b["vel_east"] == pytest.approx(102.8888)
    assert b["world"] == "real"
    assert b["precision_eligible"] is False


def test_restored_reference_is_revalidated_and_does_not_forge_prepared_status():
    original = readsb_references(envelope(), 1001)
    restored = json.loads(json.dumps(original))
    assert seeding_references({}, original, {}, "real") == seeding_references({}, restored, {}, "real")
    restored["abc123"]["lat"] = float("nan")
    restored["abc123"]["kinematics_complete"] = True
    assert not seeding_references({}, restored, {}, "real")


def test_prepared_references_still_honor_eligibility_changes():
    rows = readsb_references(envelope(), 1001)
    rows["abc123"]["reference_eligible"] = False
    assert not seeding_references({}, rows, {}, "real")
    rows = readsb_references(envelope(gs=None), 1001)
    assert not seeding_references({}, rows, {}, "real")


def test_synthetic_snapshot_never_scans_real_remote_catalogues(monkeypatch):
    class RealOnlyCache(dict):
        def items(self):
            raise AssertionError("Synthetic snapshot scanned real observations")

    monkeypatch.setattr(state, "service_adsb_cache", RealOnlyCache())
    monkeypatch.setattr(state, "external_adsb_cache", RealOnlyCache())
    monkeypatch.setattr(state, "adsb_aircraft", {})
    assert state._adsb_for_seeding("sim") == {}


def test_readsb_v2_millisecond_envelope_uses_seconds_for_seen_pos():
    payload = envelope()
    payload["now"] = 1_789_815_972_001
    rec = readsb_references(payload, 1_789_815_973)["abc123"]
    assert rec["timestamp_ms"] == 1_789_815_970_001


@pytest.mark.parametrize(
    "change",
    [
        {"mlat": ["lat", "lon"]},
        {"tisb": ["lat"]},
        {"type": "mlat"},
        {"lat": float("nan")},
        {"lon": 200},
        {"seen_pos": -1},
        {"seen_pos": None},
        {"hex": "not-an-icao"},
        {"lat": True},
    ],
)
def test_invalid_or_non_adsb_positions_never_become_truth(change):
    assert not readsb_references(envelope(**change), 1001)


def test_stale_or_future_envelope_does_not_mint_current_positions():
    assert not readsb_references(envelope(), 1100)
    assert not readsb_references(envelope(), 990)


def test_missing_kinematics_is_position_only_not_a_zero_velocity_seed():
    state.service_adsb_cache.update(readsb_references(envelope(gs=None), 1001))
    assert state.service_adsb_cache["abc123"]["gs"] is None
    assert "abc123" not in state._adsb_for_seeding("real")


def test_node_tag_legacy_altitude_units_and_observation_clock():
    tag = {"lat": 34, "lon": -82, "alt": 10000, "gs": 200, "track": 0, "timestamp": 998}
    rec = node_reference(tag, "abc123", 1000000, 1001000)
    assert rec["alt_m"] == pytest.approx(3048)
    assert rec["timestamp_ms"] == 998000
    assert rec["time_basis"] == "node_position"
    assert rec["vel_north"] == pytest.approx(102.8888)
    assert rec["reference_eligible"] is True
    assert rec["precision_eligible"] is False


@pytest.mark.parametrize("field", ["alt_baro", "gs", "track"])
def test_incomplete_node_kinematics_cannot_replace_a_complete_external_reference(field):
    state.service_adsb_cache.update(readsb_references(envelope(), 1001))
    tag = {"lat": 10, "lon": 20, "alt_baro": 10000, "gs": 200, "track": 0}
    tag.pop(field)
    state.adsb_aircraft["abc123"] = node_reference(tag, "abc123", 1000000, 1001000)
    assert state.adsb_aircraft["abc123"]["reference_eligible"] is False
    assert state._adsb_for_seeding("real")["abc123"]["lat"] == 34.88


@pytest.mark.parametrize("extra", [{"type": "mlat"}, {"tisb": ["lat"]}, {"timestamp": None}, {"timestamp": 1010}])
def test_invalid_node_sources_and_clocks_abstain(extra):
    tag = {"lat": 34, "lon": -82, "alt_baro": 10000, "gs": 200, "track": 0, **extra}
    assert node_reference(tag, "abc123", 1000000, 1001000) is None


def test_tcp_node_positions_keep_their_own_clock_and_legacy_feet_altitude():
    from services.tcp_handler import _apply_synthetic_adsb

    ts_ms = int(time.time() * 1000)
    tag = {"hex": "abc123", "lat": 34, "lon": -82, "alt": 10000, "gs": 200, "track": 0}
    tag["timestamp"] = (ts_ms - 3000) / 1000
    _apply_synthetic_adsb({"data": {"timestamp": ts_ms, "adsb": [tag]}}, "hardware-reference-test")
    rec = state._adsb_for_seeding("real")["abc123"]
    assert rec["timestamp_ms"] == ts_ms - 3000
    assert rec["alt_m"] == pytest.approx(3048)


def test_real_node_mlat_tag_cannot_enter_known_claims():
    nid = "hardware-reference-test"
    state.node_associator.register_node(nid, _NODE_CFG)
    delay, doppler = _stationary_pred(state.node_associator.node_geometries[nid])
    frame = {
        "timestamp": int(time.time() * 1000),
        "delay": [delay],
        "doppler": [doppler],
        "snr": [20],
        "adsb": [
            {"hex": "abc123", "lat": 34.88, "lon": -82.35, "alt_baro": 23000, "gs": 0, "track": 0, "type": "mlat"}
        ],
    }
    assert claim_known_targets(nid, frame) == set()
    assert not state.known_claims.get("abc123")


def test_external_truth_reaches_claiming_with_correct_units():
    state.external_adsb_cache["abc123"] = {
        "lat": 34.88,
        "lon": -82.35,
        "alt_m": 7000,
        "velocity": 100,
        "heading": 90,
        "last_seen_ms": 1000000,
        "source": "opensky",
    }
    rec = state._adsb_for_seeding("real")["abc123"]
    assert rec["vel_east"] == pytest.approx(100)
    assert rec["gs"] == pytest.approx(100 / 0.514444)
    assert rec["alt_baro"] == pytest.approx(7000 / 0.3048)
    assert not state._adsb_for_seeding("sim")


@pytest.mark.parametrize("with_reference", [False, True])
def test_incomplete_node_tag_requires_a_fresh_independent_reference(with_reference):
    nid = "hardware-reference-test"
    state.node_associator.register_node(nid, _NODE_CFG)
    delay, doppler = _stationary_pred(state.node_associator.node_geometries[nid])
    ts = int(time.time() * 1000)
    if with_reference:
        state.external_adsb_cache["abc123"] = {
            "lat": 34.88,
            "lon": -82.35,
            "alt_m": 7000,
            "velocity": 0,
            "heading": 0,
            "last_seen_ms": ts - 1000,
            "source": "opensky",
        }
    frame = {
        "timestamp": ts,
        "delay": [delay],
        "doppler": [doppler],
        "snr": [20],
        "adsb": [{"hex": "abc123", "lat": 34.88, "lon": -82.35}],
    }
    assert claim_known_targets(nid, frame) == ({0} if with_reference else set())
    if with_reference:
        fix = state.known_claims["abc123"][-1]["adsb_fix"]
        assert fix["fix_ts_ms"] == ts - 1000
        assert fix["alt_baro"] == pytest.approx(7000 / 0.3048)


def test_colliding_sim_identity_cannot_replace_real_truth():
    state.service_adsb_cache.update(readsb_references(envelope(), 1001))
    state.adsb_aircraft["abc123"] = {
        "lat": 10,
        "lon": 20,
        "alt_baro": 10000,
        "gs": 200,
        "track": 0,
        "last_seen_ms": 1000000,
        "world": "sim",
    }
    assert state._adsb_for_seeding("real")["abc123"]["lat"] == 34.88
    assert state._adsb_for_seeding("sim")["abc123"]["lat"] == 10


def test_v1_identity_claim_uses_reference_clock_and_preserves_array_alignment(monkeypatch):
    nid = "hardware-reference-test"
    state.node_associator.register_node(nid, _NODE_CFG)
    geo = state.node_associator.node_geometries[nid]
    ts = int(time.time() * 1000)
    state.external_adsb_cache["abc123"] = {
        "lat": 34.88,
        "lon": -82.35,
        "alt_m": 7000,
        "velocity": 0,
        "heading": 0,
        "last_seen_ms": ts - 1000,
        "source": "opensky",
    }
    delay, doppler = _stationary_pred(geo)
    frame = {
        "timestamp": ts,
        "delay": [delay, 500],
        "doppler": [doppler, 500],
        "snr": [20, 20],
        "adsb_hex": ["ABC123", None],
    }
    original = state._adsb_for_seeding
    snapshots = []

    def snapshot(world=None):
        snapshots.append(world)
        return original(world)

    monkeypatch.setattr(state, "_adsb_for_seeding", snapshot)
    assert claim_known_targets(nid, frame) == {0}
    assert snapshots == ["real"]
    claim = state.known_claims["abc123"][-1]
    assert claim["adsb_fix"]["fix_ts_ms"] == ts - 1000
    assert claim["node_identity"] is True
    assert strip_claimed_detections(frame, {0})["adsb_hex"] == [None]
    assert frame["adsb_hex"] == ["ABC123", None]


@pytest.mark.asyncio
async def test_service_client_reads_v2_and_preserves_source(monkeypatch):
    monkeypatch.setattr("services.tasks.adsb_service.time.time", lambda: 1001)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=envelope()))
    ) as client:
        result = await fetch_region(client, "https://example.invalid", regions_for_nodes([(34.8, -82.3)])[0])
    assert result["abc123"]["source"] == "adsb_service"


def test_real_solve_never_scores_against_a_synthetic_trail():
    from services.tasks.solve_history import _gt_for_record

    state.ground_truth_trails["abc123"] = [(34.0, -82.0, 7000, 1000)]
    assert _gt_for_record(None, 34, -82, 1000, "real")["gt_error_km"] is None
    assert _gt_for_record("abc123", 34, -82, 1000, "real")["gt_error_km"] is None


def test_world_funnel_retains_failed_attempts_without_result_geometry():
    from services.solver_report import _world_funnels

    result = _world_funnels(
        [
            {"world": "real", "n_nodes": 2, "outcome": "no_converge"},
            {"world": "sim", "outcome": "published", "gt_error_km": 0.1},
        ]
    )
    assert result["real"]["dark"]["attempts"] == 1
    assert result["real"]["dark"]["publish_rate"] == 0
    assert result["sim"]["dark"]["publish_rate"] == 1
