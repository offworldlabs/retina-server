"""The adsb-service poller: which regions it asks for, what it stores, and how it paces itself."""

import logging

import pytest

from clients.adsb_service import RateLimited
from core import state
from services.adsb_regions import regions_for_nodes
from services.tasks import adsb_fallback

_NOW_S = 1_790_000_000.0


class _Clock:
    """A monotonic clock that only moves when the poller sleeps or a fetch takes time."""

    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class _Client:
    """Answers each region from a table keyed by the region's latitude."""

    def __init__(self, answers, clock=None, cost_s=0.0):
        self.answers = answers
        self.asked: list[float] = []
        self.clock = clock
        self.cost_s = cost_s

    async def fetch_point(self, lat, lon, radius_nm):
        self.asked.append(lat)
        if self.clock is not None:
            self.clock.t += self.cost_s
        answer = self.answers.get(round(lat, 1), [])
        if isinstance(answer, Exception):
            raise answer
        return answer


def _row(hexn, captured_s, lat=34.0, lon=-84.0):
    return {
        "hex": hexn,
        "flight": "TST1",
        "lat": lat,
        "lon": lon,
        "alt_baro": 30000,
        "gs": 400.0,
        "track": 90.0,
        "captured_ms": int(captured_s * 1000),
    }


def _placed(lat, lon):
    """A config placing both ends of the pair, the way claiming needs a node."""
    return {"rx_lat": lat, "rx_lon": lon, "tx_lat": lat + 0.1, "tx_lon": lon}


def _regions(*lats):
    # Nodes a lattice cell apart, so each lands in its own region.
    return regions_for_nodes([(lat, -84.0) for lat in lats])


def _cell(lat):
    """The lattice cell a node at (lat, -84) queries under."""
    (region,) = _regions(lat)
    return (region.row, region.col)


def _stored(lat=34.0):
    """What the store holds for the region of a node at (lat, -84)."""
    return state.adsb_fallback.get(_cell(lat), {})


@pytest.fixture(autouse=True)
def _fixed_wall_clock(monkeypatch):
    monkeypatch.setattr(adsb_fallback.time, "time", lambda: _NOW_S)


def test_the_poller_runs_only_when_switched_on_with_exactly_1(monkeypatch):
    for value, expected in ((None, False), ("0", False), ("true", False), ("1", True)):
        if value is None:
            monkeypatch.delenv(adsb_fallback.ENABLED_ENV, raising=False)
        else:
            monkeypatch.setenv(adsb_fallback.ENABLED_ENV, value)
        assert adsb_fallback.enabled() is expected


async def test_a_switched_off_poller_returns_at_once(monkeypatch):
    monkeypatch.delenv(adsb_fallback.ENABLED_ENV, raising=False)
    await adsb_fallback.adsb_fallback_task()


class _StopLoop(Exception):
    pass


def _run_one_loop(monkeypatch, client):
    """Run the task until its first inter-cycle sleep; return what that sleep was asked for."""
    monkeypatch.setenv(adsb_fallback.ENABLED_ENV, "1")
    monkeypatch.setattr(adsb_fallback, "AdsbServiceClient", lambda: client)
    waits = []

    async def stop(s):
        waits.append(s)
        raise _StopLoop

    monkeypatch.setattr(adsb_fallback.asyncio, "sleep", stop)
    return waits


class _ClosingClient(_Client):
    closed = False

    async def aclose(self):
        self.closed = True


async def test_the_running_task_waits_out_the_interval_plus_any_retry_after(monkeypatch):
    state.connected_nodes["real-a"] = {
        "status": "active",
        "is_synthetic": False,
        "config": _placed(34.0, -84.0),
    }
    client = _ClosingClient({34.0: RateLimited(7.0)})
    waits = _run_one_loop(monkeypatch, client)
    with pytest.raises(_StopLoop):
        await adsb_fallback.adsb_fallback_task()
    assert waits[0] == pytest.approx(adsb_fallback.INTERVAL_S + 7.0, abs=0.5)
    assert client.closed


async def test_with_no_real_node_placed_the_store_empties_and_the_task_still_reports_alive(monkeypatch):
    state.adsb_fallback = {_cell(34.0): {"a1b2c3": {"hex": "a1b2c3", "last_seen_ms": int(_NOW_S * 1000)}}}
    client = _ClosingClient({})
    _run_one_loop(monkeypatch, client)
    with pytest.raises(_StopLoop):
        await adsb_fallback.adsb_fallback_task()
    assert client.asked == []
    assert state.adsb_fallback == {}
    assert state.task_last_success["adsb_fallback"] == _NOW_S


def test_regions_cover_connected_real_positioned_nodes_only():
    state.connected_nodes.update(
        {
            "real-a": {"status": "active", "is_synthetic": False, "config": _placed(34.0, -84.0)},
            "synth-b": {"status": "active", "is_synthetic": True, "config": _placed(40.0, -74.0)},
            "gone-c": {"status": "disconnected", "is_synthetic": False, "config": _placed(47.0, -122.0)},
            "unplaced-d": {"status": "active", "is_synthetic": False, "config": {"node_id": "unplaced-d"}},
            # Claiming refuses a node with no transmitter, so it earns no query.
            "no-tx-e": {"status": "active", "is_synthetic": False, "config": {"rx_lat": 40.0, "rx_lon": -100.0}},
        }
    )
    regions = adsb_fallback.real_node_regions()
    assert [(round(r.lat, 1), round(r.lon, 1)) for r in regions] == [(34.0, -84.0)]


async def test_a_cycle_stores_real_world_candidates_stamped_from_their_capture():
    clock = _Clock()
    client = _Client({34.0: [_row("a1b2c3", _NOW_S - 6.0)]})
    await adsb_fallback.run_cycle(client, _regions(34.0), clock=clock, sleep=clock.sleep)

    assert list(state.adsb_fallback) == [_cell(34.0)]
    rec = _stored()["a1b2c3"]
    assert rec["world"] == "real"
    assert rec["last_seen_ms"] == int((_NOW_S - 6.0) * 1000)
    # The seeding provider's derived fields are on the stored record already.
    assert rec["timestamp_ms"] == rec["last_seen_ms"]
    assert rec["alt_m"] == pytest.approx(30000 * 0.3048)
    assert "captured_ms" not in rec
    assert state.task_last_success["adsb_fallback"] == _NOW_S


async def test_each_region_keeps_its_own_answer_where_regions_overlap():
    clock = _Clock()
    client = _Client({34.0: [_row("a1b2c3", _NOW_S - 9.0)], 38.0: [_row("a1b2c3", _NOW_S - 2.0, lat=35.9)]})
    await adsb_fallback.run_cycle(client, _regions(34.0, 38.0), clock=clock, sleep=clock.sleep)
    assert _stored(34.0)["a1b2c3"]["last_seen_ms"] == int((_NOW_S - 9.0) * 1000)
    assert _stored(38.0)["a1b2c3"]["lat"] == 35.9


async def test_the_store_never_writes_into_the_node_fed_store():
    clock = _Clock()
    client = _Client({34.0: [_row("a1b2c3", _NOW_S - 1.0)]})
    await adsb_fallback.run_cycle(client, _regions(34.0), clock=clock, sleep=clock.sleep)
    assert state.adsb_aircraft == {}


async def test_requests_are_spaced_to_the_services_sustained_rate():
    clock = _Clock()
    client = _Client({})
    await adsb_fallback.run_cycle(client, _regions(30.0, 34.0, 38.0), clock=clock, sleep=clock.sleep)
    assert len(client.asked) == 3
    assert clock.sleeps == [adsb_fallback.REQUEST_SPACING_S] * 2
    assert 1.0 / adsb_fallback.REQUEST_SPACING_S <= 2.0


async def test_a_429_cuts_the_cycle_short_and_says_which_regions_went_unasked(caplog):
    clock = _Clock()
    client = _Client({30.0: [_row("a1b2c3", _NOW_S - 1.0)], 34.0: RateLimited(12.0)})
    regions = _regions(30.0, 34.0, 38.0)  # asked south to north
    before = state.adsb_fallback_truncated_cycles
    with caplog.at_level(logging.WARNING, logger=adsb_fallback.__name__):
        cycle = await adsb_fallback.run_cycle(client, regions, clock=clock, sleep=clock.sleep)

    assert cycle.retry_after_s == 12.0
    assert client.asked == [30.0, 34.0]  # the region after the 429 is never asked
    assert state.adsb_fallback_truncated_cycles == before + 1
    assert all(r.name in caplog.text for r in regions[1:])
    # What did answer is still stored.
    assert "a1b2c3" in _stored(30.0)


async def test_the_next_cycle_starts_where_a_cut_short_one_stopped():
    # Always asking busiest first would starve the same tail under a lasting limit.
    clock = _Clock()
    regions = _regions(30.0, 34.0, 38.0)
    cycle = await adsb_fallback.run_cycle(_Client({34.0: RateLimited(1.0)}), regions, clock=clock, sleep=clock.sleep)
    assert cycle.resume_at == 1
    client = _Client({})
    cycle = await adsb_fallback.run_cycle(client, regions, start=cycle.resume_at, clock=clock, sleep=clock.sleep)
    assert client.asked == [34.0, 38.0, 30.0]
    assert cycle.resume_at == 0


async def test_a_cycle_that_overruns_its_interval_stops_and_logs(caplog):
    clock = _Clock()
    client = _Client({}, clock=clock, cost_s=adsb_fallback.INTERVAL_S)  # one slow answer eats the cycle
    regions = _regions(34.0, 38.0)
    before = state.adsb_fallback_truncated_cycles
    with caplog.at_level(logging.WARNING, logger=adsb_fallback.__name__):
        await adsb_fallback.run_cycle(client, regions, clock=clock, sleep=clock.sleep)
    assert len(client.asked) == 1
    assert state.adsb_fallback_truncated_cycles == before + 1
    assert regions[1].name in caplog.text


async def test_a_long_retry_after_is_waited_out_only_up_to_a_ceiling():
    clock = _Clock()
    client = _Client({34.0: RateLimited(3600.0)})
    cycle = await adsb_fallback.run_cycle(client, _regions(34.0), clock=clock, sleep=clock.sleep)
    assert cycle.retry_after_s == adsb_fallback.MAX_RETRY_AFTER_S


async def test_a_cycle_whose_last_answer_lands_past_the_interval_says_so(caplog):
    clock = _Clock()
    client = _Client({}, clock=clock, cost_s=adsb_fallback.INTERVAL_S + 1)
    with caplog.at_level(logging.WARNING, logger=adsb_fallback.__name__):
        await adsb_fallback.run_cycle(client, _regions(34.0), clock=clock, sleep=clock.sleep)
    assert "overran" in caplog.text


async def test_a_failed_region_costs_only_itself():
    clock = _Clock()
    client = _Client({34.0: RuntimeError("boom"), 38.0: [_row("d4e5f6", _NOW_S - 1.0)]})
    before = state.adsb_fallback_region_errors
    await adsb_fallback.run_cycle(client, _regions(34.0, 38.0), clock=clock, sleep=clock.sleep)
    assert "d4e5f6" in _stored(38.0)
    assert state.adsb_fallback_region_errors == before + 1


async def test_a_record_not_refreshed_this_cycle_stays_until_it_is_too_old_to_claim():
    clock = _Clock()
    max_age = adsb_fallback.MAX_FIX_AGE_S
    state.adsb_fallback = {
        _cell(34.0): {
            "keep01": {"hex": "keep01", "last_seen_ms": int((_NOW_S - max_age + 1) * 1000)},
            "drop01": {"hex": "drop01", "last_seen_ms": int((_NOW_S - max_age - 1) * 1000)},
        },
        # A cell whose nodes have all gone is not asked, so is not kept either.
        _cell(38.0): {"gone01": {"hex": "gone01", "last_seen_ms": int(_NOW_S * 1000)}},
    }
    await adsb_fallback.run_cycle(_Client({34.0: RuntimeError("down")}), _regions(34.0), clock=clock, sleep=clock.sleep)
    assert state.adsb_fallback == {_cell(34.0): {"keep01": state.adsb_fallback[_cell(34.0)]["keep01"]}}


async def test_an_answer_too_old_to_claim_is_not_stored():
    clock = _Clock()
    client = _Client({34.0: [_row("a1b2c3", _NOW_S - adsb_fallback.MAX_FIX_AGE_S - 1)]})
    await adsb_fallback.run_cycle(client, _regions(34.0), clock=clock, sleep=clock.sleep)
    assert state.adsb_fallback == {}


def test_the_dashboard_reports_the_fallback_store_and_its_troubles(client, monkeypatch):
    # Aircraft, not records: one seen by two regions counts once.
    state.adsb_fallback = {(1, 2): {"a1b2c3": {}, "d4e5f6": {}}, (3, 4): {"a1b2c3": {}}}
    monkeypatch.setattr(state, "adsb_fallback_truncated_cycles", 3)
    monkeypatch.setattr(state, "adsb_fallback_region_errors", 2)
    body = client.get("/api/test/dashboard").json()
    assert body["adsb_fallback"] == {"cached": 2, "truncated_cycles": 3, "region_errors": 2}
    # The node-fed count is not inflated by it.
    assert body["pipeline"]["adsb_aircraft"] == 0


async def test_a_cycle_where_nothing_answered_is_not_a_success():
    clock = _Clock()
    client = _Client({34.0: RuntimeError("down")})
    await adsb_fallback.run_cycle(client, _regions(34.0), clock=clock, sleep=clock.sleep)
    assert "adsb_fallback" not in state.task_last_success
