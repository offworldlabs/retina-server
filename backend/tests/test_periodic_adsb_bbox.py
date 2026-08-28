"""_fetch_external_adsb's bounding box — services/tasks/periodic.py."""

import asyncio

import pytest

from core import state
from services.tasks import periodic


class _FakeResponse:
    status_code = 200

    def __init__(self, states):
        self._states = states

    def json(self):
        return {"states": self._states}


class _FakeOpenSkyClient:
    """Stands in for httpx.AsyncClient, capturing the bbox each call sends."""

    is_closed = False

    def __init__(self, states):
        self._states = states
        self.calls: list[dict] = []

    async def get(self, url, params=None):
        self.calls.append(params)
        return _FakeResponse(self._states)


# One minimal OpenSky state vector: [icao, callsign, origin, ts, ts, lon, lat, alt, ...].
# Non-empty so _fetch_external_adsb takes its early return rather than falling
# through to the adsb.lol fallback, which would otherwise make a real request.
_ONE_STATE = [["abc123", "TST1", None, None, None, -84.5, 33.85, 1000.0, False, 100.0, 90.0]]


@pytest.fixture(autouse=True)
def _clean_nodes():
    yield
    with state.connected_nodes_lock:
        for node_id in ("test-bbox-positionless", "test-bbox-positioned"):
            state.connected_nodes.pop(node_id, None)


def test_a_positionless_node_does_not_break_the_bounding_box(monkeypatch):
    """A fleet with one positionless node and one positioned node still
    computes a bounding box, from the positioned node alone: min()/max() over
    a lats/lons list that has picked up a None raises TypeError, and the
    caller's blanket except would then never refresh external_adsb_cache
    again for any node."""
    with state.connected_nodes_lock:
        state.connected_nodes["test-bbox-positionless"] = {
            "status": "active",
            "is_synthetic": False,
            "config": {"rx_lat": None, "rx_lon": None},
        }
        state.connected_nodes["test-bbox-positioned"] = {
            "status": "active",
            "is_synthetic": False,
            "config": {"rx_lat": 33.9, "rx_lon": -84.6},
        }

    fake_client = _FakeOpenSkyClient(_ONE_STATE)
    monkeypatch.setattr(periodic, "_opensky_client", fake_client)

    rate_limited = asyncio.run(periodic._fetch_external_adsb())

    assert rate_limited is False
    assert len(fake_client.calls) == 1
    params = fake_client.calls[0]
    assert params["lamin"] == pytest.approx(33.9 - periodic.OPENSKY_BUFFER_DEG)
    assert params["lamax"] == pytest.approx(33.9 + periodic.OPENSKY_BUFFER_DEG)
    assert params["lomin"] == pytest.approx(-84.6 - periodic.OPENSKY_BUFFER_DEG)
    assert params["lomax"] == pytest.approx(-84.6 + periodic.OPENSKY_BUFFER_DEG)


def test_all_nodes_positionless_skips_the_fetch(monkeypatch):
    with state.connected_nodes_lock:
        state.connected_nodes["test-bbox-positionless"] = {
            "status": "active",
            "is_synthetic": False,
            "config": {"rx_lat": None, "rx_lon": None},
        }

    fake_client = _FakeOpenSkyClient(_ONE_STATE)
    monkeypatch.setattr(periodic, "_opensky_client", fake_client)

    rate_limited = asyncio.run(periodic._fetch_external_adsb())

    assert rate_limited is False
    assert fake_client.calls == []
