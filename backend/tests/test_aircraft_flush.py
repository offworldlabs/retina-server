"""Tests for aircraft flush — _real_only_dict and broadcast_aircraft."""

import os

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

import asyncio
import time

import orjson
import pytest

from core import state  # noqa: E402
from services.tasks import aircraft_flush  # noqa: E402
from services.tasks.aircraft_flush import (  # noqa: E402
    _real_only_dict,
    broadcast_aircraft,
)


@pytest.fixture(autouse=True)
def _cleanup():
    old_nodes = dict(state.connected_nodes)
    old_json = state.latest_aircraft_json
    old_bytes = state.latest_aircraft_json_bytes
    yield
    state.connected_nodes.clear()
    state.connected_nodes.update(old_nodes)
    state.latest_aircraft_json = old_json
    state.latest_aircraft_json_bytes = old_bytes


class TestRealOnlyDict:
    def test_filters_synthetic_nodes(self):
        state.connected_nodes["real-1"] = {"is_synthetic": False}
        state.connected_nodes["synth-1"] = {"is_synthetic": True}

        data = {
            "now": 1000,
            "aircraft": [
                {"node_id": "real-1", "hex": "A"},
                {"node_id": "synth-1", "hex": "B"},
            ],
            "detection_arcs": [
                {"node_id": "real-1"},
                {"node_id": "synth-1"},
            ],
        }
        result = _real_only_dict(data)

        assert len(result["aircraft"]) == 1
        assert result["aircraft"][0]["hex"] == "A"
        assert len(result["detection_arcs"]) == 1

    def test_multinode_includes_if_any_real(self):
        state.connected_nodes["real-1"] = {"is_synthetic": False}
        state.connected_nodes["synth-1"] = {"is_synthetic": True}

        data = {
            "now": 1000,
            "aircraft": [
                {
                    "node_id": "synth-1",
                    "hex": "MN",
                    "multinode": True,
                    "contributing_node_ids": ["synth-1", "real-1"],
                },
            ],
            "detection_arcs": [],
        }
        result = _real_only_dict(data)
        assert len(result["aircraft"]) == 1
        assert result["aircraft"][0]["hex"] == "MN"

    def test_empty_aircraft(self):
        data = {"now": 0, "aircraft": [], "detection_arcs": []}
        result = _real_only_dict(data)
        assert result["aircraft"] == []
        assert result["messages"] == 0


class TestBroadcastAircraft:
    def test_updates_state_bytes(self):
        data = {"now": 123, "aircraft": [], "detection_arcs": [], "ground_truth": {}}

        asyncio.get_event_loop().run_until_complete(broadcast_aircraft(data))

        # The frame kept on state is the unredacted one the owner filter reads;
        # the bytes are always rebuilt, because substitution allocates.
        assert state.latest_aircraft_json is data
        assert state.latest_aircraft_json_bytes == orjson.dumps(data)


class _StubWS:
    """Minimal websocket double: records payloads, optionally never returns."""

    def __init__(self, delay_s: float = 0.0):
        self.delay_s = delay_s
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, payload: str):
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        self.sent.append(payload)

    async def close(self):
        self.closed = True


class TestBroadcastFanOut:
    """One wedged client must cost one timeout, not one timeout per client.

    The sends used to run in three serial `await asyncio.wait_for(..., 5.0)`
    loops, so N stuck clients held the flush task for up to N x 5 s — and with
    it every feed-store GC that hung off the feed build.  They are fanned out
    with asyncio.gather now, which bounds the whole broadcast at one timeout.
    """

    TIMEOUT_S = 0.1

    @pytest.fixture(autouse=True)
    def _fast_timeout(self, monkeypatch):
        monkeypatch.setattr(aircraft_flush, "WS_SEND_TIMEOUT_S", self.TIMEOUT_S)
        state.ws_clients.clear()
        state.ws_live_clients.clear()
        state.ws_owner_clients.clear()
        yield
        state.ws_clients.clear()
        state.ws_live_clients.clear()
        state.ws_owner_clients.clear()

    @staticmethod
    def _data():
        return {"now": 123, "aircraft": [], "detection_arcs": [], "ground_truth": {}}

    def _broadcast(self):
        return asyncio.run(broadcast_aircraft(self._data()))

    def test_slow_client_is_dropped_and_the_others_are_served(self):
        fast_a, fast_b, slow = _StubWS(), _StubWS(), _StubWS(delay_s=30.0)
        state.ws_clients.update({fast_a, fast_b, slow})
        before = state.ws_send_timeouts

        self._broadcast()

        assert len(fast_a.sent) == 1
        assert len(fast_b.sent) == 1
        assert slow.sent == []
        assert state.ws_clients == {fast_a, fast_b}
        assert slow.closed is True
        assert state.ws_send_timeouts == before + 1

    def test_many_slow_clients_cost_one_timeout_not_one_each(self):
        slow = [_StubWS(delay_s=30.0) for _ in range(3)]
        fast = _StubWS()
        state.ws_clients.update(slow + [fast])
        before = state.ws_send_timeouts

        started = time.monotonic()
        self._broadcast()
        elapsed = time.monotonic() - started

        # Serialised, this would be 3 x TIMEOUT_S before the fast client is
        # even reached; fanned out it is one.
        assert elapsed < 3 * self.TIMEOUT_S
        assert len(fast.sent) == 1
        assert state.ws_clients == {fast}
        assert state.ws_send_timeouts == before + 3

    def test_a_raising_client_is_dropped_without_counting_a_timeout(self):
        class _Broken(_StubWS):
            async def send_text(self, payload):
                raise RuntimeError("connection gone")

        broken, ok = _Broken(), _StubWS()
        state.ws_clients.update({broken, ok})
        before = state.ws_send_timeouts

        self._broadcast()

        assert state.ws_clients == {ok}
        assert broken.closed is True
        assert state.ws_send_timeouts == before

    def test_owner_clients_are_fanned_out_with_their_filtered_payloads(self):
        fast, slow = _StubWS(), _StubWS(delay_s=30.0)
        # Synthetic-prefixed ids, so the published payload still carries the
        # entry: an unregistered real id has no node_ref and is dropped at the
        # publication boundary, which is not what this test is about.
        state.ws_owner_clients[fast] = {"test-a"}
        state.ws_owner_clients[slow] = {"test-b"}
        data = {
            "now": 5,
            "aircraft": [{"node_id": "test-a", "hex": "aaa"}],
            "detection_arcs": [],
            "ground_truth": {},
        }
        before = state.ws_send_timeouts

        asyncio.run(broadcast_aircraft(data))

        assert [a["hex"] for a in orjson.loads(fast.sent[0])["aircraft"]] == ["aaa"]
        assert list(state.ws_owner_clients) == [fast]
        assert slow.closed is True
        assert state.ws_send_timeouts == before + 1

    def test_live_clients_are_fanned_out_too(self):
        fast, slow = _StubWS(), _StubWS(delay_s=30.0)
        state.ws_live_clients.update({fast, slow})
        before = state.ws_send_timeouts

        self._broadcast()

        assert len(fast.sent) == 1
        assert state.ws_live_clients == {fast}
        assert slow.closed is True
        assert state.ws_send_timeouts == before + 1
