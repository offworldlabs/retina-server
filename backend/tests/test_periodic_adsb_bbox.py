"""_fetch_external_adsb's node positions, in services/tasks/periodic.py.

The query regions are built from connected_nodes' configs, which since 1.1.3
may carry a null position.  Two layers drop one: the node loop here, and
regions_for_nodes via is_position_absent / is_usable.  These pin the outcome
rather than either mechanism, so removing one layer leaves them green and
removing both fails them with the TypeError being guarded against, raised in
cell_of.  That is the failure worth a test: _fetch_external_adsb's caller
swallows the exception and never retries, so one unplaced node would cost the
whole fleet its ADS-B ground truth silently and for good.
"""

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
    """Stands in for httpx.AsyncClient, capturing each box a call requests."""

    is_closed = False

    def __init__(self, states):
        self._states = states
        self.calls: list[dict] = []

    async def get(self, url, params=None):
        self.calls.append(params)
        return _FakeResponse(self._states)


# One minimal OpenSky state vector: [icao, callsign, origin, ts, ts, lon, lat, alt, ...].
_ONE_STATE = [["abc123", "TST1", None, None, None, -84.5, 33.85, 1000.0, False, 100.0, 90.0]]

_POSITIONED = (33.9, -84.6)


@pytest.fixture(autouse=True)
def _clean_nodes():
    yield
    with state.connected_nodes_lock:
        for node_id in ("test-bbox-positionless", "test-bbox-positioned"):
            state.connected_nodes.pop(node_id, None)


@pytest.fixture
def _no_fallback(monkeypatch):
    """Keep the adsb.lol fallback off the network for a partially covered region."""

    async def _none(_uncovered):
        return {}, set()

    monkeypatch.setattr(periodic, "_fetch_adsb_lol", _none)


def _add_node(node_id, lat, lon):
    with state.connected_nodes_lock:
        state.connected_nodes[node_id] = {
            "status": "active",
            "is_synthetic": False,
            "config": {"rx_lat": lat, "rx_lon": lon},
        }


def test_a_positionless_node_does_not_cost_the_fleet_its_regions(monkeypatch, _no_fallback):
    """A mixed fleet still queries, from the positioned node alone."""
    _add_node("test-bbox-positionless", None, None)
    _add_node("test-bbox-positioned", *_POSITIONED)

    fake_client = _FakeOpenSkyClient(_ONE_STATE)
    monkeypatch.setattr(periodic, "_opensky_client", fake_client)

    rate_limited = asyncio.run(periodic._fetch_external_adsb())

    assert rate_limited is False
    assert fake_client.calls, "the positioned node should still have been queried"
    lat, lon = _POSITIONED
    assert any(p["lamin"] <= lat <= p["lamax"] and p["lomin"] <= lon <= p["lomax"] for p in fake_client.calls), (
        f"no requested box covers the positioned node: {fake_client.calls}"
    )


def test_all_nodes_positionless_skips_the_fetch(monkeypatch, _no_fallback):
    _add_node("test-bbox-positionless", None, None)

    fake_client = _FakeOpenSkyClient(_ONE_STATE)
    monkeypatch.setattr(periodic, "_opensky_client", fake_client)

    rate_limited = asyncio.run(periodic._fetch_external_adsb())

    assert rate_limited is False
    assert fake_client.calls == []
