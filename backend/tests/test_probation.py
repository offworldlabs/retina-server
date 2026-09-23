"""Tests for the probation fence around polled blah2 radars.

With the fence on, which it is unless POLLED_RADAR_PROBATION_ENABLED is `0`, a
polled node that has not graduated feeds its own analytics and tracker and
nothing else: no solver input, no archive write, no aircraft-feed mark and no
public payload. These pin the cache that answers the question, the privacy
boundary it rides on and each cut in the frame path, and that with the fence
switched off a polled node flows like any other.
"""

import asyncio
import queue
from datetime import UTC, datetime

import orjson
import pytest
from retina_analytics.association import AssociationRound
from sqlalchemy import update

from core import state
from core.frame_queue import ShardedFrameQueue
from core.nodes import Node, PolledRadar
from core.users import async_session_maker
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from services import detection_mirror, frame_processor, node_auth, node_refs, probation, publication
from services.blah2_poller import CUSTODY_CLASS
from services.frame_processor import process_one_frame
from services.publication import is_private, private_node_ids, public_aircraft_payload, public_summaries
from services.tasks import frame_loop
from tests.node_helpers import register_test_node

_FLAG = "POLLED_RADAR_PROBATION_ENABLED"

_PROBATION = "bla0a1b2c3d"
_GRADUATED = "bla1f2e3d4c"
_UNKNOWN = "blaffff0000"
_FLEET = "ret1a2b3c4d"
_PEER = "site-b"

_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
_PLACED_CFG = {
    "rx_lat": 34.0,
    "rx_lon": -84.0,
    "tx_lat": 33.8,
    "tx_lon": -83.8,
    "fc_hz": 195e6,
    "max_range_km": 150.0,
}
_ADSB = [{"hex": "abc123", "lat": 33.9, "lon": -84.6, "alt_baro": 35000}]


@pytest.fixture()
def fence_on(monkeypatch):
    """The default: nothing set."""
    monkeypatch.delenv(_FLAG, raising=False)


@pytest.fixture()
def fence_off(monkeypatch):
    monkeypatch.setenv(_FLAG, "0")


def _run(coro) -> None:
    asyncio.run(coro)
    # asyncio.run() clears the loop on exit (3.12); conftest's _clean_db
    # restores one for the same reason.
    asyncio.set_event_loop(asyncio.new_event_loop())


def _seed_polled(publication_choice: str = "public", **trust: str) -> None:
    """A nodes row and a polled_radars row per node id, in the given trust state."""

    async def _go():
        async with async_session_maker() as session:
            for nid in trust:
                session.add(
                    Node(
                        node_id=nid,
                        node_ref=node_auth.mint_node_ref(),
                        board_model="blah2",
                        publication=publication_choice,
                    )
                )
            await session.flush()
            for nid, trust_state in trust.items():
                session.add(
                    PolledRadar(
                        node_id=nid,
                        endpoint_raw=f"http://{nid}.example.com:3000/",
                        scheme="http",
                        host=f"{nid}.example.com",
                        port=3000,
                        endpoint_key=f"{nid}.example.com:3000",
                        config_fingerprint="a" * 64,
                        probe_passed_at=_NOW,
                        endpoint_changed_at=_NOW,
                        trust_state=trust_state,
                    )
                )
            await session.commit()

    _run(_go())
    probation.invalidate()
    publication.invalidate()
    node_refs._reset_for_tests()


def _set_trust(node_id: str, trust_state: str) -> None:
    """Rewrite one row's trust state without touching either cache."""

    async def _go():
        async with async_session_maker() as session:
            await session.execute(
                update(PolledRadar).where(PolledRadar.node_id == node_id).values(trust_state=trust_state)
            )
            await session.commit()

    _run(_go())


def _failing_query(calls: list):
    def _query():
        calls.append(1)
        raise RuntimeError("database is gone")

    return _query


def _frame(ts_ms: int = 1_700_000_000_000) -> dict:
    return {
        "timestamp": ts_ms,
        "delay": [50.0, 52.0, 54.0],
        "doppler": [10.0, 15.0, 20.0],
        "snr": [20.0, 21.0, 22.0],
    }


def _payload() -> dict:
    return {
        "now": 1.0,
        "messages": 3,
        "aircraft": [
            {"hex": "AAA111", "lat": 1.0, "lon": 2.0, "node_id": _PROBATION},
            {"hex": "BBB222", "lat": 3.0, "lon": 4.0, "node_id": _FLEET},
            {
                "hex": "mnCCC333",
                "lat": 5.0,
                "lon": 6.0,
                "multinode": True,
                "contributing_node_ids": [_PROBATION, _FLEET],
            },
        ],
        "detection_arcs": [
            {"hex": "AAA111", "node_id": _PROBATION, "ambiguity_arc": [[1.0, 2.0], [1.1, 2.1]]},
            {"hex": "BBB222", "node_id": _FLEET, "ambiguity_arc": [[3.0, 4.0], [3.1, 4.1]]},
        ],
        "detecting_nodes": {"AAA111": [_PROBATION], "BBB222": [_FLEET], "mnCCC333": [_PROBATION, _FLEET]},
    }


# ── The cache ────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("fence_on")
class TestInProbation:
    def test_a_fleet_node_is_never_on_probation_and_costs_no_query(self, monkeypatch):
        calls = []
        monkeypatch.setattr(probation, "_query", _failing_query(calls))
        assert not probation.in_probation(_FLEET)
        assert not probation.in_probation("test-node")
        assert not probation.in_probation(None)
        assert not probation.in_probation("")
        assert calls == []

    def test_a_polled_node_on_probation_is_gated(self):
        _seed_polled(**{_PROBATION: "probation"})
        assert probation.in_probation(_PROBATION)

    def test_a_graduated_polled_node_is_not(self):
        _seed_polled(**{_GRADUATED: "graduated"})
        assert not probation.in_probation(_GRADUATED)

    def test_any_state_but_graduated_is_gated(self):
        _seed_polled(**{_PROBATION: "suspended"})
        assert probation.in_probation(_PROBATION)

    def test_a_polled_id_with_no_row_is_gated(self):
        _seed_polled(**{_GRADUATED: "graduated"})
        assert probation.in_probation(_UNKNOWN)

    def test_a_cold_failure_gates_every_polled_node(self, monkeypatch):
        _seed_polled(**{_GRADUATED: "graduated"})
        probation._reset_for_tests()
        monkeypatch.setattr(probation, "_query", _failing_query([]))
        assert probation.in_probation(_GRADUATED)

    def test_a_failure_after_a_good_read_gates_every_polled_node(self, monkeypatch):
        """A stale graduated set could hold open a node that has re-entered probation."""
        _seed_polled(**{_GRADUATED: "graduated"})
        assert not probation.in_probation(_GRADUATED)
        monkeypatch.setattr(probation, "_query", _failing_query([]))
        probation.invalidate()
        assert probation.in_probation(_GRADUATED)

    def test_a_failure_backs_off_rather_than_querying_every_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr(probation, "_query", _failing_query(calls))
        for _ in range(3):
            assert probation.in_probation(_GRADUATED)
        assert len(calls) == 1

    def test_the_answer_is_cached_within_the_ttl(self):
        _seed_polled(**{_GRADUATED: "graduated"})
        assert not probation.in_probation(_GRADUATED)
        _set_trust(_GRADUATED, "probation")
        assert not probation.in_probation(_GRADUATED)

    def test_invalidate_re_applies_the_gate_without_a_restart(self):
        _seed_polled(**{_GRADUATED: "graduated"})
        assert not probation.in_probation(_GRADUATED)
        _set_trust(_GRADUATED, "probation")
        probation.invalidate()
        assert probation.in_probation(_GRADUATED)

    def test_the_cache_expires_on_its_own(self, monkeypatch):
        _seed_polled(**{_GRADUATED: "graduated"})
        assert not probation.in_probation(_GRADUATED)
        _set_trust(_GRADUATED, "probation")
        monkeypatch.setattr(probation, "_expires_at", 0.0)
        assert probation.in_probation(_GRADUATED)


# ── The privacy boundary ─────────────────────────────────────────────────────


@pytest.mark.usefixtures("fence_on")
class TestProbationIsPrivate:
    def test_a_polled_node_on_probation_is_private(self):
        _seed_polled(**{_PROBATION: "probation"})
        assert is_private(_PROBATION)
        assert _PROBATION in private_node_ids()

    def test_a_graduated_polled_node_is_public(self):
        _seed_polled(**{_GRADUATED: "graduated"})
        assert not is_private(_GRADUATED)
        assert _GRADUATED not in private_node_ids()

    def test_a_polled_id_with_no_row_is_private(self):
        assert is_private(_UNKNOWN)
        assert _UNKNOWN in private_node_ids()

    def test_a_fleet_node_with_no_row_stays_public(self):
        assert not is_private(_FLEET)

    def test_a_graduated_node_its_owner_registered_private_stays_private(self):
        _seed_polled(publication_choice="private", **{_GRADUATED: "graduated"})
        assert is_private(_GRADUATED)

    def test_it_is_listed_for_an_owner_merge(self):
        """The analytics route intersects the set with the caller's nodes."""
        _seed_polled(**{_PROBATION: "probation", _GRADUATED: "graduated"})
        assert private_node_ids() & {_PROBATION, _GRADUATED} == {_PROBATION}

    def test_the_aircraft_payload_drops_it_when_nothing_else_is_private(self):
        _seed_polled(**{_PROBATION: "probation"})
        out = public_aircraft_payload(_payload())
        assert [ac["hex"] for ac in out["aircraft"]] == ["BBB222", "mnCCC333"]
        assert out["aircraft"][1]["contributing_node_ids"] == [_FLEET]
        assert [arc["node_id"] for arc in out["detection_arcs"]] == [_FLEET]
        assert out["detecting_nodes"] == {"BBB222": [_FLEET], "mnCCC333": [_FLEET]}
        assert _PROBATION not in orjson.dumps(out).decode()

    def test_an_unregistered_polled_id_is_dropped_from_the_payload(self):
        payload = _payload()
        payload["aircraft"][0]["node_id"] = _UNKNOWN
        out = public_aircraft_payload(payload)
        assert _UNKNOWN not in orjson.dumps(out).decode()

    def test_analytics_summaries_drop_it(self):
        _seed_polled(**{_PROBATION: "probation", _GRADUATED: "graduated"})
        summaries = {nid: {"node_id": nid} for nid in (_PROBATION, _GRADUATED, _FLEET)}
        assert set(public_summaries(summaries)) == {_GRADUATED, _FLEET}

    def test_the_node_listing_leaves_it_out(self):
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        _seed_polled(**{_PROBATION: "probation", _GRADUATED: "graduated"})
        cfg = {"rx_lat": 34.0, "rx_lon": -82.0, "rx_alt_ft": 100.0}
        for nid in (_PROBATION, _GRADUATED):
            state.connected_nodes[nid] = {"status": "active", "config": {**cfg, "node_id": nid}}
        try:
            _refresh_analytics_and_nodes()
        finally:
            for nid in (_PROBATION, _GRADUATED):
                state.connected_nodes.pop(nid, None)

        body = orjson.loads(state.latest_nodes_bytes)
        assert node_refs.ref_for(_PROBATION) not in body["nodes"]
        assert node_refs.ref_for(_GRADUATED) in body["nodes"]
        assert body["total"] == 1

    def test_re_entry_makes_it_private_without_a_restart(self):
        _seed_polled(**{_GRADUATED: "graduated"})
        assert not is_private(_GRADUATED)
        _set_trust(_GRADUATED, "probation")
        probation.invalidate()
        assert is_private(_GRADUATED)

    def test_a_probation_read_failure_keeps_it_private(self, monkeypatch):
        _seed_polled(**{_GRADUATED: "graduated"})
        assert not is_private(_GRADUATED)
        monkeypatch.setattr(probation, "_query", _failing_query([]))
        probation.invalidate()
        assert is_private(_GRADUATED)


# ── The frame path ───────────────────────────────────────────────────────────


class _FrameFence:
    @pytest.fixture(autouse=True)
    def _fence(self, monkeypatch):
        """Record every way a frame could leave its own node.

        submit_tracks_round is stubbed to hand back a solver-shaped input naming
        the submitting node and `self.peer`, so a frame that reaches association
        reaches the queue: the fence under test is frame_processor's, not the
        associator's.
        """
        self.solver_queue = queue.Queue()
        monkeypatch.setattr(state, "solver_queue", self.solver_queue)
        self.peer = _PEER
        self.rounds = []

        def _round(node_id, _tracks, ts_ms):
            self.rounds.append(node_id)
            s_in = {
                "measurements": [
                    {"node_id": node_id, "delay_us": 10.0, "doppler_hz": 5.0, "snr": 15.0},
                    {"node_id": self.peer, "delay_us": 12.0, "doppler_hz": 4.0, "snr": 14.0},
                ],
                "n_nodes": 2,
                "timestamp_ms": ts_ms,
            }
            return AssociationRound(pairs=[], anchored_inputs=[s_in], claims=[])

        monkeypatch.setattr(state.node_associator, "submit_tracks_round", _round)
        self.claimed = []

        def _claim(node_id, _frame, follow_claimed):
            self.claimed.append(node_id)
            return set()

        monkeypatch.setattr(frame_processor, "claim_known_targets", _claim)
        monkeypatch.setattr(state, "KNOWN_LANE_MODE", "shadow")
        state.aircraft_dirty = False
        self.default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        yield
        for nid in (_PROBATION, _GRADUATED, _UNKNOWN, _FLEET):
            state.connected_nodes.pop(nid, None)
            state.node_pipelines.pop(nid, None)
            state.node_associator.unregister_node(nid)
            state.node_analytics.retire_node(nid)
        state.adsb_aircraft.pop("abc123", None)

    def _process(self, node_id: str, **extra) -> None:
        if node_id not in state.connected_nodes:
            register_test_node(node_id, _PLACED_CFG)
        process_one_frame(node_id, {**_frame(), **extra}, self.default)

    def _assert_fed_nothing_shared(self, node_id: str) -> None:
        assert self.solver_queue.empty()
        assert node_id not in self.rounds
        assert node_id not in self.claimed
        assert node_id not in frame_processor._archive_buffer

    def _assert_flowed(self, node_id: str) -> None:
        s_in, _cfgs, _at = self.solver_queue.get_nowait()
        assert s_in["measurements"][0]["node_id"] == node_id
        assert node_id in self.rounds
        assert node_id in self.claimed
        assert len(frame_processor._archive_buffer[node_id]) == 1

    def _reset_records(self, node_id: str) -> None:
        self.rounds.clear()
        self.claimed.clear()
        frame_processor._archive_buffer.pop(node_id, None)


@pytest.mark.usefixtures("fence_on")
class TestFrameGate(_FrameFence):
    def test_a_probation_frame_feeds_its_own_analytics_and_tracker(self):
        _seed_polled(**{_PROBATION: "probation"})
        self._process(_PROBATION)
        assert state.node_analytics.metrics[_PROBATION].total_frames == 1
        assert state.node_pipelines[_PROBATION]._frame_count == 1

    def test_a_probation_frame_feeds_nothing_shared(self):
        _seed_polled(**{_PROBATION: "probation"})
        self._process(_PROBATION)
        self._assert_fed_nothing_shared(_PROBATION)

    def test_a_probation_frames_adsb_list_stays_out_of_the_shared_cache(self):
        _seed_polled(**{_PROBATION: "probation"})
        self._process(_PROBATION, adsb=_ADSB)
        assert "abc123" not in state.adsb_aircraft
        assert state.aircraft_dirty is False

    def test_a_graduated_polled_frame_flows_like_a_fleet_frame(self):
        _seed_polled(**{_GRADUATED: "graduated"})
        self._process(_GRADUATED, adsb=_ADSB)
        self._assert_flowed(_GRADUATED)
        assert "abc123" in state.adsb_aircraft

    def test_a_graduated_polled_frame_reaches_the_archive_with_its_custody_class_and_epoch(self):
        _seed_polled(**{_GRADUATED: "graduated"})
        try:
            self._process(_GRADUATED, signing_mode=CUSTODY_CLASS, epoch=4)
            archived = frame_processor._archive_buffer[_GRADUATED][-1]
            assert (archived["signing_mode"], archived["epoch"]) == (CUSTODY_CLASS, 4)
        finally:
            frame_processor._archive_buffer.pop(_GRADUATED, None)

    def test_a_fleet_frame_is_untouched_and_never_reads_the_trust_table(self, monkeypatch):
        calls = []
        monkeypatch.setattr(probation, "_query", _failing_query(calls))
        self._process(_FLEET)
        self._assert_flowed(_FLEET)
        assert calls == []

    def test_an_unregistered_polled_id_is_gated(self):
        self._process(_UNKNOWN)
        self._assert_fed_nothing_shared(_UNKNOWN)

    def test_a_probation_read_failure_gates_a_graduated_node(self, monkeypatch):
        _seed_polled(**{_GRADUATED: "graduated"})
        monkeypatch.setattr(probation, "_query", _failing_query([]))
        probation.invalidate()
        self._process(_GRADUATED)
        self._assert_fed_nothing_shared(_GRADUATED)

    def test_re_entry_gates_the_next_frame_without_a_restart(self):
        _seed_polled(**{_GRADUATED: "graduated"})
        self._process(_GRADUATED)
        self._assert_flowed(_GRADUATED)

        _set_trust(_GRADUATED, "probation")
        probation.invalidate()
        self._reset_records(_GRADUATED)
        self._process(_GRADUATED)
        self._assert_fed_nothing_shared(_GRADUATED)

    def test_a_fleet_round_naming_a_probation_node_is_not_solved(self):
        """A node back on probation can leave tracks in the associator that a
        fleet node's round still pairs with."""
        _seed_polled(**{_PROBATION: "probation"})
        self.peer = _PROBATION
        self._process(_FLEET)
        assert _FLEET in self.rounds
        assert self.solver_queue.empty()


async def _drain_one(monkeypatch, node_id: str) -> None:
    """Run one frame through a frame worker, with process_one_frame stubbed out."""
    frames = ShardedFrameQueue(maxsize=10, shards=1)
    monkeypatch.setattr(state, "frame_queue", frames)
    seen = []
    monkeypatch.setattr(frame_loop, "process_one_frame", lambda nid, _frame, _pipeline: seen.append(nid))
    state.aircraft_dirty = False
    frames.put_nowait((node_id, {}))
    task = asyncio.create_task(frame_loop.frame_processor_loop(object(), 0))
    try:
        for _ in range(500):
            if seen and frames.empty():
                break
            await asyncio.sleep(0.01)
        # One more turn so the worker finishes the iteration it is in.
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert seen == [node_id]


@pytest.mark.usefixtures("fence_on")
class TestFrameLoop:
    async def test_a_probation_frame_does_not_mark_the_feed_dirty(self, monkeypatch):
        await _drain_one(monkeypatch, _UNKNOWN)
        assert state.aircraft_dirty is False

    async def test_a_fleet_frame_still_does(self, monkeypatch):
        await _drain_one(monkeypatch, _FLEET)
        assert state.aircraft_dirty is True


def _mirrored(node_id: str) -> list[str]:
    """The node ids an armed mirror queues after offering one frame from `node_id`."""
    from routes.node_schemas import DetectionFrame

    detection_mirror.configure_from_env({"DETECTION_MIRROR_URL": "https://sink.invalid/", "DETECTION_MIRROR_KEY": "k"})
    try:
        frame = DetectionFrame(
            t=1753900000.123,
            seq=1,
            boot_id="k3n8v2qp71ab",
            config_version=1,
            delay=[12.4],
            doppler=[-118.0],
            snr=[14.2],
            adsb_hex=["4ca1f2"],
        )
        detection_mirror.offer(node_id, frame)
        return [nid for nid, _ in detection_mirror.drain()]
    finally:
        detection_mirror.configure_from_env({})


@pytest.mark.usefixtures("fence_on")
class TestDetectionMirror:
    """The mirror posts frames into other environments' solve and archive."""

    def test_a_probation_frame_is_not_mirrored(self):
        _seed_polled(**{_PROBATION: "probation"})
        assert _mirrored(_PROBATION) == []

    def test_a_graduated_or_fleet_frame_is(self):
        _seed_polled(**{_GRADUATED: "graduated"})
        assert _mirrored(_GRADUATED) == [_GRADUATED]
        assert _mirrored(_FLEET) == [_FLEET]


# ── The switch ───────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("fence_off")
class TestFenceSwitchedOff(_FrameFence):
    def test_no_polled_node_is_on_probation_and_the_trust_table_is_not_read(self, monkeypatch):
        calls = []
        monkeypatch.setattr(probation, "_query", _failing_query(calls))
        assert not probation.in_probation(_PROBATION)
        assert not probation.in_probation(_UNKNOWN)
        assert calls == []

    def test_a_polled_node_follows_its_owners_choice(self):
        _seed_polled(**{_PROBATION: "probation"})
        assert not is_private(_PROBATION)
        assert not is_private(_UNKNOWN)
        assert private_node_ids() == frozenset()

    def test_a_polled_node_registered_private_is_private(self):
        _seed_polled(publication_choice="private", **{_PROBATION: "probation"})
        assert is_private(_PROBATION)

    def test_the_public_payload_carries_it_unchanged(self):
        _seed_polled(**{_PROBATION: "probation"})
        payload = _payload()
        assert public_aircraft_payload(payload) is payload
        summaries = {_PROBATION: {"node_id": _PROBATION}}
        assert public_summaries(summaries) is summaries

    def test_a_probation_frame_flows_like_a_fleet_frame(self):
        _seed_polled(**{_PROBATION: "probation"})
        self._process(_PROBATION, adsb=_ADSB)
        self._assert_flowed(_PROBATION)
        assert "abc123" in state.adsb_aircraft
        assert state.aircraft_dirty is True

    def test_a_fleet_round_naming_a_polled_node_is_solved(self):
        _seed_polled(**{_PROBATION: "probation"})
        self.peer = _PROBATION
        self._process(_FLEET)
        self._assert_flowed(_FLEET)

    def test_a_polled_frame_is_mirrored(self):
        _seed_polled(**{_PROBATION: "probation"})
        assert _mirrored(_PROBATION) == [_PROBATION]

    async def test_a_polled_frame_marks_the_feed_dirty(self, monkeypatch):
        await _drain_one(monkeypatch, _UNKNOWN)
        assert state.aircraft_dirty is True

    def test_switching_it_back_on_gates_the_next_frame_without_a_restart(self, monkeypatch):
        _seed_polled(**{_PROBATION: "probation"})
        self._process(_PROBATION)
        self._assert_flowed(_PROBATION)
        assert not is_private(_PROBATION)

        monkeypatch.delenv(_FLAG)
        self._reset_records(_PROBATION)
        self._process(_PROBATION)
        self._assert_fed_nothing_shared(_PROBATION)
        assert is_private(_PROBATION)


class TestTheSwitch:
    """Fails closed: only an exact `0` switches the fence off."""

    def test_unset_means_on(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        assert probation.enabled()
        assert probation.in_probation(_UNKNOWN)

    @pytest.mark.parametrize("value", ["", "1", "false", "off", "no", " 0", "00"])
    def test_any_value_but_zero_means_on(self, monkeypatch, value):
        monkeypatch.setenv(_FLAG, value)
        assert probation.enabled()
        assert probation.in_probation(_UNKNOWN)

    def test_zero_switches_it_off(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "0")
        assert not probation.enabled()
        assert not probation.in_probation(_UNKNOWN)


class TestHealthReportsTheSwitch:
    def test_on_by_default(self, client, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        assert client.get("/api/health").json()["polled_radar_probation"] is True

    def test_off_when_switched_off(self, client, monkeypatch):
        monkeypatch.setenv(_FLAG, "0")
        assert client.get("/api/health").json()["polled_radar_probation"] is False
