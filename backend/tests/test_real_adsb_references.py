"""Real-feed clocks, provenance, units, world isolation and v1 identity plumbing."""

import time

import httpx
import pytest

from core import state
from services.adsb_regions import regions_for_nodes
from services.adsb_truth import readsb_references
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


def test_v1_identity_claim_uses_reference_clock_and_preserves_array_alignment():
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
    assert claim_known_targets(nid, frame) == {0}
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
