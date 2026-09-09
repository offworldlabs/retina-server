"""External ADS-B truth is fetched per region, not for one fleet-wide extent.

86cb5p1jy site 1.  The fault this pins: reducing the fleet to a single centre
put the query in the open North Atlantic and starved every consumer of truth,
while adsb_truth_fetcher went on reporting healthy.

The recurring shape of that fault is one bad input taking down truth for
everyone, so most of what follows checks that a failure stays confined to the
region that caused it and the rest of the fleet still gets its answer.
"""

import asyncio
import logging
import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from config.constants import KNOTS_TO_MS
from core import state
from services.adsb_regions import Region, regions_for_nodes
from services.tasks import periodic

ATLANTA = (33.939182, -84.388)
MASSACHUSETTS = (42.5, -71.5)
SACRAMENTO = (38.6, -121.5)


def _connect(positions):
    state.connected_nodes.clear()
    for i, (lat, lon) in enumerate(positions):
        state.connected_nodes[f"test-region-{i}"] = {
            "status": "connected",
            "is_synthetic": False,
            "config": {"rx_lat": lat, "rx_lon": lon},
        }


def _state_vector(icao, lat, lon, alt=3000.0, velocity=220.0, heading=90.0, captured=None):
    """An OpenSky state vector. The fetcher reads indices 0, 3, 5, 6, 7, 9 and 10.

    Index 3 is time_position, which the entry's `last_seen_ms` is taken from, so
    it defaults to now: 0 is a real epoch-0 stamp and would make every vector
    built here stale before any age gate saw it.
    """
    captured = time.time() if captured is None else captured
    return [icao, "TEST123 ", "United States", captured, captured, lon, lat, alt, False, velocity, heading]


class _StubResponse:
    def __init__(self, status_code, payload=None, headers=None, raises=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


class _StubClient:
    """Stands in for the pooled httpx client, handing back canned responses.

    A list is served in call order; a callable is handed the request params, so
    a test on a region split across the antimeridian can answer per box without
    depending on the order the two go out in.  An entry that is an exception is
    raised instead, standing for a transport failure (timeout, DNS, refused
    connection).
    """

    is_closed = False

    def __init__(self, responses):
        self._responses = responses if callable(responses) else list(responses)
        self.calls = []
        self.close_count = 0

    async def get(self, url, params=None):
        self.calls.append(params)
        item = self._responses(params) if callable(self._responses) else self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        self.close_count += 1


class _ConcurrencyProbeClient(_StubClient):
    """Records how many requests were ever in flight at once."""

    def __init__(self, responses):
        super().__init__(responses)
        self.in_flight = 0
        self.max_in_flight = 0

    async def get(self, url, params=None):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Yields the loop, so a sequential caller would never overlap two.
            await asyncio.sleep(0.01)
            return await super().get(url, params)
        finally:
            self.in_flight -= 1


@contextmanager
def _stub_opensky(responses):
    """Install a stub OpenSky client, including for the rebuild after a transport error."""
    stub = _StubClient(responses)
    original = periodic._opensky_client
    periodic._opensky_client = stub
    try:
        with patch.object(periodic.httpx, "AsyncClient", lambda **kw: stub):
            yield stub
    finally:
        periodic._opensky_client = original


class _StubLolClient:
    def __init__(self, aircraft, last_status):
        self.areas = []
        self.last_status = last_status
        self._aircraft = aircraft

    def fetch_all(self):
        return self._aircraft


@contextmanager
def _stub_lol(stub):
    original = periodic._adsb_lol_client
    periodic._adsb_lol_client = stub
    try:
        yield stub
    finally:
        periodic._adsb_lol_client = original


def _lol_covering_all(cache=None):
    """A `_fetch_adsb_lol` stub that covers every region it is handed."""

    def _fetch(regions):
        return dict(cache or {}), {r.name for r in regions}

    return AsyncMock(side_effect=_fetch)


@pytest.fixture(autouse=True)
def _clean():
    yield
    state.connected_nodes.clear()
    state.external_adsb_cache = {}


class TestRegionFetch:
    """The cache-replacement decision, with both providers stubbed at the boundary."""

    @pytest.mark.asyncio
    async def test_every_metro_is_represented_in_the_merged_cache(self):
        _connect([ATLANTA, MASSACHUSETTS, SACRAMENTO])
        queried = []

        def fake_fetch(regions):
            queried.extend(r.name for r in regions)
            cache = {f"ac{i}": {"lat": 0.0, "lon": 0.0, "source": "adsb_lol"} for i in range(len(regions))}
            return cache, {r.name for r in regions}

        with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(side_effect=fake_fetch)):
            with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=({}, set(), set(), False))):
                await periodic._fetch_external_adsb()

        assert len(set(queried)) == 3
        assert len(state.external_adsb_cache) == 3

    @pytest.mark.asyncio
    async def test_an_empty_successful_fetch_clears_rather_than_hiding(self):
        # The silent failure: `if fallback_cache:` left a stale cache in place
        # and logged nothing, so the fault looked like a healthy task.
        state.external_adsb_cache = {"stale": {"lat": 1.0, "lon": 1.0}}
        _connect([ATLANTA])

        with patch.object(periodic, "_fetch_adsb_lol", _lol_covering_all()):
            with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=({}, set(), set(), False))):
                await periodic._fetch_external_adsb()

        assert state.external_adsb_cache == {}

    @pytest.mark.asyncio
    async def test_a_total_failure_keeps_the_last_good_cache(self):
        state.external_adsb_cache = {"keep": {"lat": 1.0, "lon": 1.0}}
        _connect([ATLANTA])

        with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(return_value=({}, set()))):
            with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=({}, set(), set(), False))):
                await periodic._fetch_external_adsb()

        assert "keep" in state.external_adsb_cache

    @pytest.mark.asyncio
    async def test_the_fallback_throwing_outright_still_keeps_opensky_s_regions(self):
        _connect([ATLANTA, SACRAMENTO])
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        opensky = ({"abc123": {"lat": 33.9, "lon": -84.3, "source": "opensky"}}, {regions[0].name}, set(), True)

        with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(side_effect=RuntimeError("adsb.lol down"))):
            with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=opensky)):
                await periodic._fetch_external_adsb()

        assert state.external_adsb_cache["abc123"]["source"] == "opensky"


class TestRateLimitBackoff:
    """What `_fetch_external_adsb` returns, since the caller turns it into 300 s of sleep."""

    @pytest.mark.asyncio
    async def test_a_429_the_fallback_covered_costs_no_backoff(self):
        # Charging the backoff for a cycle that ended with complete truth
        # delays the next one by 420 s for nothing.
        _connect([ATLANTA, SACRAMENTO])
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        opensky = (
            {"abc123": {"lat": 33.9, "lon": -84.3, "source": "opensky"}},
            {regions[0].name},
            {regions[1].name},
            True,
        )

        with patch.object(periodic, "_fetch_adsb_lol", _lol_covering_all({"beef01": {"lat": 38.6, "lon": -121.5}})):
            with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=opensky)):
                assert await periodic._fetch_external_adsb() is False

    @pytest.mark.asyncio
    async def test_a_429_that_left_a_region_uncovered_does_arm_it(self):
        _connect([ATLANTA, SACRAMENTO])
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        opensky = (
            {"abc123": {"lat": 33.9, "lon": -84.3, "source": "opensky"}},
            {regions[0].name},
            {regions[1].name},
            True,
        )

        with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(return_value=({}, set()))):
            with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=opensky)):
                assert await periodic._fetch_external_adsb() is True

    @pytest.mark.asyncio
    async def test_another_region_s_gap_does_not_charge_the_credit_budget(self):
        # Two independent gaps: the region OpenSky refused for credit is
        # covered by the fallback, while a second region fails at OpenSky's
        # transport and at the fallback too.  Nothing about the credit budget
        # caused that second gap, so the 300 s backoff has not been earned.
        _connect([ATLANTA, SACRAMENTO])
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        refused, broken = regions[0], regions[1]
        opensky = ({}, set(), {refused.name}, False)

        def fake_lol(uncovered):
            return {"beef01": {"lat": 33.9, "lon": -84.3, "source": "adsb_lol"}}, {refused.name}

        with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(side_effect=fake_lol)):
            with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=opensky)):
                assert await periodic._fetch_external_adsb() is False
        assert broken.name != refused.name

    @pytest.mark.asyncio
    async def test_the_fallback_failing_outright_leaves_the_429_charged(self):
        _connect([ATLANTA])
        regions = regions_for_nodes([ATLANTA])
        opensky = ({}, set(), {regions[0].name}, False)

        with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(side_effect=RuntimeError("adsb.lol down"))):
            with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=opensky)):
                assert await periodic._fetch_external_adsb() is True


class TestOpenSkyFetch:
    """`_fetch_opensky` itself, against canned HTTP responses."""

    @pytest.mark.asyncio
    async def test_states_are_parsed_and_stamped_opensky(self):
        regions = regions_for_nodes([ATLANTA])
        captured = time.time() - 30.0
        vector = _state_vector("abc123", 33.9, -84.3, alt=3000.0, velocity=220.0, heading=90.0, captured=captured)

        with _stub_opensky([_StubResponse(200, {"states": [vector]})]):
            cache, covered, credit_refused, _ = await periodic._fetch_opensky(regions)

        assert covered == {regions[0].name}
        assert credit_refused == set()
        assert cache["abc123"] == {
            "lat": 33.9,
            "lon": -84.3,
            "alt_m": 3000.0,
            "velocity": 220.0,
            "heading": 90.0,
            "last_seen_ms": int(captured * 1000),
            "source": "opensky",
        }

    @pytest.mark.asyncio
    async def test_a_region_nothing_was_asked_for_is_not_reported_covered(self):
        # Coverage is read off the answers rather than assumed and cleared by
        # failures: a region with no boxes has been asked nothing, and passing
        # it as covered on an empty cache would leave it unqueried by either
        # provider and silently absent from the truth cache.
        region = Region(name="r0c0", row=0, col=0, lat=33.9, lon=-84.3, radius_nm=100, boxes=(), n_nodes=1)

        with _stub_opensky([]) as stub:
            cache, covered, credit_refused, _ = await periodic._fetch_opensky([region])

        assert stub.calls == []
        assert cache == {}
        assert covered == set()  # so the caller hands it to the fallback
        assert credit_refused == set()

    @pytest.mark.asyncio
    async def test_the_bounding_box_of_each_region_is_what_is_queried(self):
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        boxes = [b for r in regions for b in r.boxes]
        responses = [_StubResponse(200, {"states": []}) for _ in boxes]

        with _stub_opensky(responses) as stub:
            await periodic._fetch_opensky(regions)

        assert [c["lamin"] for c in stub.calls] == [b.lamin for b in boxes]
        assert [c["lomax"] for c in stub.calls] == [b.lomax for b in boxes]

    @pytest.mark.parametrize("states", [[], None])  # `states` is nullable in the schema
    @pytest.mark.asyncio
    async def test_an_empty_sky_counts_as_covered(self, states):
        # Deliberate: a working query over quiet airspace is an answer, and
        # treating it as failure is what let the stale cache masquerade as fresh.
        regions = regions_for_nodes([ATLANTA])

        with _stub_opensky([_StubResponse(200, {"states": states})]):
            cache, covered, _, _ = await periodic._fetch_opensky(regions)

        assert cache == {}
        assert covered == {regions[0].name}

    @pytest.mark.parametrize(
        "payload",
        [
            {"states": "abc123"},  # a string iterates into characters, one skipped vector apiece
            {"states": {"abc123": []}},  # a mapping iterates into its keys
            {"time": 1712000000},  # the documented key absent altogether
        ],
    )
    @pytest.mark.asyncio
    async def test_a_states_field_that_is_not_a_list_leaves_the_box_to_the_fallback(self, payload, caplog):
        # An undocumented `states` that happens to be iterable and truthy read as
        # quiet airspace: the box passed as covered with nothing in it, which is
        # indistinguishable from an empty sky and skips the fallback that exists
        # for exactly this payload.
        regions = regions_for_nodes([ATLANTA])

        with caplog.at_level(logging.WARNING):
            with _stub_opensky([_StubResponse(200, payload)]):
                cache, covered, _, _ = await periodic._fetch_opensky(regions)

        assert cache == {}
        assert covered == set()
        assert regions[0].name in caplog.text

    @pytest.mark.asyncio
    async def test_a_429_costs_only_its_own_region_and_logs_when_credits_return(self, caplog):
        # The requests are in flight together, so a 429 cannot call the others
        # off; it names its own region as refused and leaves it to the fallback,
        # while a sibling that was served still counts as covered.
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        responses = [
            _StubResponse(429, headers={"X-Rate-Limit-Retry-After-Seconds": "1800"}),
            _StubResponse(200, {"states": [_state_vector("abc123", 38.6, -121.5)]}),
        ]

        with caplog.at_level(logging.INFO):
            with _stub_opensky(responses) as stub:
                cache, covered, credit_refused, _ = await periodic._fetch_opensky(regions)

        # Which region was refused, not merely that one was: the caller can
        # only tell an earned backoff from an unrelated gap by the name.
        assert credit_refused == {regions[0].name}
        assert covered == {regions[1].name}
        assert "abc123" in cache
        assert len(stub.calls) == 2
        assert "1800" in caplog.text

    @pytest.mark.asyncio
    async def test_the_regions_are_asked_concurrently(self):
        # Sequential awaits made the OpenSky phase cost one timeout per region
        # before the fallback was reached at all; the phase must be bounded by
        # roughly one timeout instead.
        regions = regions_for_nodes([ATLANTA, MASSACHUSETTS, SACRAMENTO])
        stub = _ConcurrencyProbeClient([_StubResponse(200, {"states": []}) for _ in regions])
        original = periodic._opensky_client
        periodic._opensky_client = stub
        try:
            _, covered, _, _ = await periodic._fetch_opensky(regions)
        finally:
            periodic._opensky_client = original

        assert stub.max_in_flight == min(len(regions), periodic._OPENSKY_MAX_CONCURRENT)
        assert covered == {r.name for r in regions}

    @pytest.mark.asyncio
    async def test_the_burst_is_capped_rather_than_tracking_the_region_count(self):
        # Every region leaving at once makes the burst size follow
        # ADSB_MAX_REGIONS_PER_CYCLE, which is what a per-second limiter or a
        # connection-rate defence reacts to.  The in-flight count must stay put
        # as the region count grows past it.
        n = periodic._OPENSKY_MAX_CONCURRENT + 2
        regions = regions_for_nodes([(30.0, -120.0 + 10.0 * i) for i in range(n)])
        assert len(regions) == n  # each position must land in its own lattice cell
        stub = _ConcurrencyProbeClient([_StubResponse(200, {"states": []}) for _ in regions])
        original = periodic._opensky_client
        periodic._opensky_client = stub
        try:
            _, covered, _, _ = await periodic._fetch_opensky(regions)
        finally:
            periodic._opensky_client = original

        assert stub.max_in_flight == periodic._OPENSKY_MAX_CONCURRENT
        assert covered == {r.name for r in regions}  # and every region is still asked

    @pytest.mark.asyncio
    async def test_a_non_200_leaves_that_region_uncovered_and_says_so(self, caplog):
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        responses = [
            _StubResponse(503),
            _StubResponse(200, {"states": [_state_vector("abc123", 38.6, -121.5)]}),
        ]

        with caplog.at_level(logging.WARNING):
            with _stub_opensky(responses) as stub:
                _, covered, _, _ = await periodic._fetch_opensky(regions)

        assert covered == {regions[1].name}
        assert len(stub.calls) == 2
        assert "503" in caplog.text and regions[0].name in caplog.text

    @pytest.mark.asyncio
    async def test_a_transport_error_does_not_abandon_the_regions_behind_it(self, caplog):
        # The regression this guards: returning on the first transport error
        # meant a region that always times out permanently starved every region
        # ordered after it, with no fallback and no log line.
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        responses = [
            httpx.ConnectTimeout("timed out"),
            _StubResponse(200, {"states": [_state_vector("abc123", 38.6, -121.5)]}),
        ]

        with caplog.at_level(logging.WARNING):
            with _stub_opensky(responses) as stub:
                cache, covered, _, _ = await periodic._fetch_opensky(regions)
                # A minority failure is the network's, not the pool's: throwing
                # the pool away would cost every region a fresh handshake next
                # cycle while most of them were being served.
                assert stub.close_count == 0
                assert periodic._opensky_client is stub

        assert len(stub.calls) == 2
        assert covered == {regions[1].name}
        assert "abc123" in cache
        assert regions[0].name in caplog.text

    @pytest.mark.asyncio
    async def test_every_region_failing_at_the_transport_does_discard_the_pool(self):
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        responses = [httpx.ConnectTimeout("timed out") for _ in regions]

        with _stub_opensky(responses) as stub:
            _, covered, _, _ = await periodic._fetch_opensky(regions)
            assert periodic._opensky_client is None

        assert covered == set()
        assert stub.close_count == 1  # the dead client is closed, not merely dropped

    @pytest.mark.asyncio
    async def test_one_malformed_state_vector_costs_only_itself(self):
        # icao24 is a string per the schema.  A vector carrying something else
        # must not take the region's other aircraft down with it.
        regions = regions_for_nodes([ATLANTA])
        payload = {
            "states": [
                [12345, "BAD1 ", "United States", 0, 0, -84.4, 33.8, 3000.0, False, 220.0, 90.0],
                _state_vector("abc123", 33.9, -84.3),
            ]
        }

        with _stub_opensky([_StubResponse(200, payload)]):
            cache, covered, _, _ = await periodic._fetch_opensky(regions)

        assert covered == {regions[0].name}
        assert list(cache) == ["abc123"]

    @pytest.mark.parametrize(
        "short",
        [
            ["abc123"],  # too short to index at all
            ["abc123", "SHORT1 ", "United States", 0, 0, -84.4, 33.8],  # every field but altitude
            "abc123",  # a string where the vector was promised
            None,  # and something with no length at all
        ],
    )
    @pytest.mark.asyncio
    async def test_a_state_vector_too_short_to_index_costs_only_itself(self, short):
        # An IndexError here would be caught region-wide, so one truncated
        # vector would hand a good region to the fallback and throw away every
        # aircraft already read from it.  Length is checked with the icao24
        # type, under one rule, so both cost only the vector that breaks it.
        regions = regions_for_nodes([ATLANTA])
        payload = {"states": [short, _state_vector("abc123", 33.9, -84.3)]}

        with _stub_opensky([_StubResponse(200, payload)]):
            cache, covered, _, _ = await periodic._fetch_opensky(regions)

        assert covered == {regions[0].name}
        assert list(cache) == ["abc123"]

    @pytest.mark.parametrize(
        ("payload", "raises"),
        [
            (None, ValueError("Expecting value: line 1 column 1")),  # an HTML interstitial behind a 200
            ([], None),  # a bare list where the documented object was promised
        ],
    )
    @pytest.mark.asyncio
    async def test_an_unreadable_200_leaves_the_region_to_the_fallback(self, payload, raises, caplog):
        regions = regions_for_nodes([ATLANTA])

        with caplog.at_level(logging.WARNING):
            with _stub_opensky([_StubResponse(200, payload, raises=raises)]):
                cache, covered, _, _ = await periodic._fetch_opensky(regions)

        assert covered == set()
        assert cache == {}
        assert regions[0].name in caplog.text


class TestSplitRegionFetch:
    """A band straddling the antimeridian is two boxes, so it is two requests.

    Both must land for the region to count as covered, so a part-answered
    region goes to the fallback whole; the aircraft the answered box did return
    are kept all the same, and are all the region has where the fallback fails
    too.
    """

    FIJI = (10.0, 179.9)

    def _split_regions(self):
        regions = regions_for_nodes([self.FIJI])
        assert [len(r.boxes) for r in regions] == [2]  # sanity: this position does split
        return regions

    @pytest.mark.asyncio
    async def test_both_boxes_are_asked_for_and_merge_into_one_region(self):
        regions = self._split_regions()

        def responder(params):
            hex_id = "abc123" if params["lomin"] == -180.0 else "def456"
            return _StubResponse(200, {"states": [_state_vector(hex_id, 10.0, params["lomax"])]})

        with _stub_opensky(responder) as stub:
            cache, covered, credit_refused, _ = await periodic._fetch_opensky(regions)

        assert {(c["lomin"], c["lomax"]) for c in stub.calls} == {(b.lomin, b.lomax) for b in regions[0].boxes}
        assert covered == {regions[0].name}
        assert credit_refused == set()
        assert sorted(cache) == ["abc123", "def456"]

    @pytest.mark.asyncio
    async def test_one_box_refused_credit_leaves_the_whole_region_uncovered(self):
        regions = self._split_regions()

        def responder(params):
            if params["lomin"] == -180.0:
                return _StubResponse(429, headers={"X-Rate-Limit-Retry-After-Seconds": "1800"})
            return _StubResponse(200, {"states": [_state_vector("abc123", 10.0, 179.95)]})

        with _stub_opensky(responder) as stub:
            cache, covered, credit_refused, _ = await periodic._fetch_opensky(regions)

        assert len(stub.calls) == 2
        assert covered == set()
        assert credit_refused == {regions[0].name}
        # Uncovered, so the fallback is still asked for the region whole, but
        # the half that answered is not thrown away to say so.
        assert list(cache) == ["abc123"]

    @pytest.mark.asyncio
    async def test_the_half_that_answered_survives_the_fallback_failing_too(self):
        # Both providers short of the region is what they exist to cover for
        # each other, and it is the case where discarding the answered box
        # leaves the region contributing nothing at all.  The truth cache
        # carries no completeness contract, so half a band is thinner truth,
        # not wrong truth.
        _connect([self.FIJI])
        self._split_regions()

        def responder(params):
            if params["lomax"] == 180.0:
                return httpx.ConnectTimeout("timed out")
            return _StubResponse(200, {"states": [_state_vector("abc123", 10.0, -179.95)]})

        with _stub_opensky(responder):
            with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(side_effect=RuntimeError("adsb.lol down"))):
                await periodic._fetch_external_adsb()

        assert state.external_adsb_cache["abc123"]["source"] == "opensky"

    @pytest.mark.asyncio
    async def test_a_truthfully_empty_box_replaces_the_cache_though_everything_else_failed(self):
        # The cycle's only answer is a box reporting quiet airspace: it is kept
        # out of `covered` because its sibling failed, and it leaves nothing in
        # the cache.  Reading the replacement off either would hold the last
        # cycle's aircraft over a fresh "nothing here", which is the stale-truth
        # fault in miniature.
        state.external_adsb_cache = {"stale": {"lat": 1.0, "lon": 1.0}}
        _connect([self.FIJI])
        self._split_regions()

        def responder(params):
            if params["lomax"] == 180.0:
                return httpx.ConnectTimeout("timed out")
            return _StubResponse(200, {"states": []})

        with _stub_opensky(responder):
            with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(return_value=({}, set()))):
                await periodic._fetch_external_adsb()

        assert state.external_adsb_cache == {}

    @pytest.mark.asyncio
    async def test_a_half_covered_region_goes_to_the_fallback(self):
        _connect([self.FIJI])
        regions = self._split_regions()
        handed = []

        def responder(params):
            if params["lomax"] == 180.0:
                return httpx.ConnectTimeout("timed out")
            return _StubResponse(200, {"states": [_state_vector("abc123", 10.0, -179.95)]})

        def fake_lol(uncovered):
            handed.extend(r.name for r in uncovered)
            return {"beef01": {"lat": 10.0, "lon": 179.9, "source": "adsb_lol"}}, {r.name for r in uncovered}

        with _stub_opensky(responder):
            with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(side_effect=fake_lol)):
                await periodic._fetch_external_adsb()

        assert handed == [regions[0].name]
        assert state.external_adsb_cache["beef01"]["source"] == "adsb_lol"
        # The two providers' answers compose: the fallback covers the region
        # whole, over the box OpenSky did answer rather than instead of it.
        assert state.external_adsb_cache["abc123"]["source"] == "opensky"

    @pytest.mark.asyncio
    async def test_one_box_failing_at_the_transport_keeps_the_pool(self):
        # The pool is discarded only where every request failed at the
        # transport, and a split region makes two: one live answer is proof the
        # client itself works, whatever became of its sibling.
        regions = self._split_regions()

        def responder(params):
            if params["lomin"] == -180.0:
                return httpx.ConnectTimeout("timed out")
            return _StubResponse(200, {"states": []})

        with _stub_opensky(responder) as stub:
            _, covered, _, _ = await periodic._fetch_opensky(regions)
            assert periodic._opensky_client is stub

        assert covered == set()
        assert stub.close_count == 0

    @pytest.mark.asyncio
    async def test_every_box_failing_at_the_transport_discards_the_pool(self):
        regions = self._split_regions()

        with _stub_opensky(lambda params: httpx.ConnectTimeout("timed out")) as stub:
            _, covered, _, _ = await periodic._fetch_opensky(regions)
            assert periodic._opensky_client is None

        assert covered == set()
        assert stub.close_count == 1
        assert len(stub.calls) == 2

    @pytest.mark.asyncio
    async def test_the_burst_stays_capped_when_regions_split(self):
        # The semaphore bounds requests rather than regions, so splitting
        # deepens the phase instead of doubling what a per-second limiter or a
        # connection-rate defence sees.
        regions = regions_for_nodes([(10.0 + 5.0 * i, 179.9) for i in range(4)])
        assert [len(r.boxes) for r in regions] == [2, 2, 2, 2]  # sanity: eight requests
        stub = _ConcurrencyProbeClient(lambda params: _StubResponse(200, {"states": []}))
        original = periodic._opensky_client
        periodic._opensky_client = stub
        try:
            _, covered, _, _ = await periodic._fetch_opensky(regions)
        finally:
            periodic._opensky_client = original

        assert stub.max_in_flight == periodic._OPENSKY_MAX_CONCURRENT
        assert len(stub.calls) == 8
        assert covered == {r.name for r in regions}


class TestAdsbLolFetch:
    """`_fetch_adsb_lol` itself, over a stubbed client."""

    @pytest.mark.asyncio
    async def test_aircraft_are_converted_and_stamped_adsb_lol(self):
        regions = regions_for_nodes([ATLANTA])
        captured_at = time.time() - 12.0
        row = {"hex": "ABC123", "lat": 33.9, "lon": -84.3, "alt_baro": 10000, "gs": 420, "track": 90}
        stub = _StubLolClient([row | {"captured_at": captured_at}], {regions[0].name: True})

        with _stub_lol(stub):
            cache, covered = await periodic._fetch_adsb_lol(regions)

        assert covered == {regions[0].name}
        # Feet and knots on the wire, metres and m/s in the cache, and the row's
        # own capture time rather than the poll's.
        assert cache["abc123"] == {
            "lat": 33.9,
            "lon": -84.3,
            "alt_m": pytest.approx(3048.0),
            "velocity": pytest.approx(420 * KNOTS_TO_MS),
            "heading": 90,
            "last_seen_ms": int(captured_at * 1000),
            "source": "adsb_lol",
        }

    @pytest.mark.asyncio
    async def test_areas_are_named_for_their_region_so_the_client_cache_keys_line_up(self):
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        stub = _StubLolClient([], {r.name: True for r in regions})

        with _stub_lol(stub):
            await periodic._fetch_adsb_lol(regions)

        assert [a["name"] for a in stub.areas] == [r.name for r in regions]

    @pytest.mark.asyncio
    async def test_only_the_regions_that_answered_are_reported_covered(self, caplog):
        # A bare "something worked" flag let the caller log every region it had
        # handed over as covered when one of them was.
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        stub = _StubLolClient([], {regions[0].name: True, regions[1].name: False})

        with caplog.at_level(logging.WARNING):
            with _stub_lol(stub):
                _, covered = await periodic._fetch_adsb_lol(regions)

        assert covered == {regions[0].name}
        assert regions[1].name in caplog.text

    @pytest.mark.asyncio
    async def test_every_region_failing_covers_nothing(self):
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        stub = _StubLolClient([], {r.name: False for r in regions})

        with _stub_lol(stub):
            _, covered = await periodic._fetch_adsb_lol(regions)

        assert covered == set()


class TestProviderHandover:
    """Only the regions OpenSky missed go to adsb.lol, and both merge into one cache."""

    @pytest.mark.parametrize(
        ("payload", "raises"),
        [
            (None, ValueError("not json")),  # an HTML interstitial behind a 200
            ({"states": "abc123"}, None),  # a `states` of some undocumented type
        ],
    )
    @pytest.mark.asyncio
    async def test_a_malformed_opensky_200_still_reaches_the_fallback(self, payload, raises):
        # The regression this guards: parsing outside the try raised out of the
        # whole fetch, so the fallback that exists for exactly this was skipped
        # and the cache went stale every 120 s.
        _connect([ATLANTA])
        entry = {"lat": 33.9, "lon": -84.3, "source": "adsb_lol"}

        with _stub_opensky([_StubResponse(200, payload, raises=raises)]):
            with patch.object(periodic, "_fetch_adsb_lol", _lol_covering_all({"beef01": entry})):
                await periodic._fetch_external_adsb()

        assert state.external_adsb_cache["beef01"]["source"] == "adsb_lol"

    @pytest.mark.asyncio
    async def test_only_the_uncovered_regions_are_handed_to_the_fallback(self):
        _connect([ATLANTA, SACRAMENTO])
        regions = regions_for_nodes([ATLANTA, SACRAMENTO])
        handed = []

        def fake_lol(uncovered):
            handed.extend(r.name for r in uncovered)
            return {"beef01": {"lat": 33.9, "lon": -84.3, "source": "adsb_lol"}}, {r.name for r in uncovered}

        responses = [
            httpx.ConnectTimeout("timed out"),
            _StubResponse(200, {"states": [_state_vector("abc123", 38.6, -121.5)]}),
        ]
        with _stub_opensky(responses):
            with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(side_effect=fake_lol)):
                await periodic._fetch_external_adsb()

        assert handed == [regions[0].name]
        assert state.external_adsb_cache["abc123"]["source"] == "opensky"
        assert state.external_adsb_cache["beef01"]["source"] == "adsb_lol"

    @pytest.mark.asyncio
    async def test_an_aircraft_both_providers_saw_keeps_the_entry_that_names_its_supplier(self):
        _connect([ATLANTA, SACRAMENTO])
        responses = [
            httpx.ConnectTimeout("timed out"),
            _StubResponse(200, {"states": [_state_vector("abc123", 38.6, -121.5)]}),
        ]
        overlap = {"abc123": {"lat": 0.0, "lon": 0.0, "source": "adsb_lol"}}

        with _stub_opensky(responses):
            with patch.object(periodic, "_fetch_adsb_lol", _lol_covering_all(overlap)):
                await periodic._fetch_external_adsb()

        assert state.external_adsb_cache["abc123"]["source"] == "opensky"
        assert state.external_adsb_cache["abc123"]["lat"] == 38.6

    @pytest.mark.asyncio
    async def test_the_summary_names_only_the_regions_the_fallback_actually_covered(self, caplog):
        # An operator reads this line as the coverage the cache holds, so it
        # must not report every region handed over on the strength of one.
        _connect([ATLANTA, MASSACHUSETTS, SACRAMENTO])

        def fake_lol(uncovered):
            return {"beef01": {"lat": 33.9, "lon": -84.3, "source": "adsb_lol"}}, {uncovered[0].name}

        with caplog.at_level(logging.INFO):
            with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(side_effect=fake_lol)):
                with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=({}, set(), set(), False))):
                    await periodic._fetch_external_adsb()

        regions = regions_for_nodes([ATLANTA, MASSACHUSETTS, SACRAMENTO])
        summary = next(r for r in caplog.records if "External ADS-B: cached" in r.msg)
        assert summary.args[-2] == "none"  # OpenSky covered nothing
        assert summary.args[-1] == [regions[0].name]

    @pytest.mark.asyncio
    async def test_both_providers_key_the_cache_in_the_same_case(self):
        # The dedup above only holds while both sides agree on case: adsb.lol
        # is lowercased on the way in, so OpenSky must be too.
        regions = regions_for_nodes([ATLANTA])

        with _stub_opensky([_StubResponse(200, {"states": [_state_vector("ABC123", 33.9, -84.3)]})]):
            cache, _, _, _ = await periodic._fetch_opensky(regions)

        assert list(cache) == ["abc123"]


class TestNodesWithoutUsablePositions:
    """A node that cannot be placed must cost only itself its truth."""

    @pytest.mark.asyncio
    async def test_a_fleet_with_no_positions_at_all_says_so(self, caplog):
        # HTTP-ingest registration writes `{"node_id": ...}` as the whole
        # config, which is truthy but carries no fix, so an all-HTTP fleet
        # would otherwise stamp success having queried nothing.
        state.connected_nodes.clear()
        state.connected_nodes["test-http-1"] = {
            "status": "connected",
            "is_synthetic": False,
            "config": {"node_id": "test-http-1"},
        }

        with caplog.at_level(logging.WARNING):
            with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=({}, set(), set(), False))) as opensky:
                assert await periodic._fetch_external_adsb() is False

        opensky.assert_not_called()
        assert "none carries a position" in caplog.text

    @pytest.mark.asyncio
    async def test_a_null_coordinate_pair_reads_as_unpositioned_not_as_rubbish(self, caplog):
        # A v1 registration may carry the keys with null values; that is the
        # same "no position" as omitting them, not a coordinate to complain
        # about, and it must not reach the lattice.
        state.connected_nodes.clear()
        state.connected_nodes["test-null-fix"] = {
            "status": "connected",
            "is_synthetic": False,
            "config": {"rx_lat": None, "rx_lon": None},
        }

        with caplog.at_level(logging.WARNING):
            with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=({}, set(), set(), False))) as opensky:
                assert await periodic._fetch_external_adsb() is False

        opensky.assert_not_called()
        assert "none carries a position" in caplog.text
        assert "unusable node position" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_node_missing_one_coordinate_is_not_placed_on_the_prime_meridian(self, caplog):
        # Coalescing the missing half to 0.0 put the node off West Africa,
        # where it looks usable: it claimed a region of empty ocean every
        # cycle, one of the eight the cap allows, and got no truth itself.
        _connect([ATLANTA])
        state.connected_nodes["test-half-fix"] = {
            "status": "connected",
            "is_synthetic": False,
            "config": {"rx_lat": 33.9, "rx_lon": None},
        }
        queried = []

        def fake_fetch(regions):
            queried.extend(r.name for r in regions)
            return {}, {r.name for r in regions}

        with caplog.at_level(logging.WARNING):
            with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(side_effect=fake_fetch)):
                with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=({}, set(), set(), False))):
                    await periodic._fetch_external_adsb()

        assert queried == [regions_for_nodes([ATLANTA])[0].name]
        assert "only one of rx_lat/rx_lon" in caplog.text

    @pytest.mark.asyncio
    async def test_one_unusable_position_does_not_starve_the_rest(self, caplog):
        # `/api/radar/detections/bulk` writes its config dict through
        # unvalidated and json.loads accepts the NaN literal, so this is a
        # reachable POST, not a hypothetical.
        _connect([ATLANTA, SACRAMENTO])
        state.connected_nodes["test-rubbish"] = {
            "status": "connected",
            "is_synthetic": False,
            "config": {"rx_lat": float("nan"), "rx_lon": float("nan")},
        }
        queried = []

        def fake_fetch(regions):
            queried.extend(r.name for r in regions)
            return {"abc123": {"lat": 33.9, "lon": -84.3, "source": "adsb_lol"}}, {r.name for r in regions}

        with caplog.at_level(logging.WARNING):
            with patch.object(periodic, "_fetch_adsb_lol", AsyncMock(side_effect=fake_fetch)):
                with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=({}, set(), set(), False))):
                    await periodic._fetch_external_adsb()

        assert len(queried) == 2
        assert state.external_adsb_cache["abc123"]["lat"] == 33.9
        assert "unusable node position" in caplog.text


class TestRangeBeyondTheQueryMargin:
    """ADSB_NODE_RANGE_MARGIN_KM assumes a fleet; node_config permits up to 1000 km.

    A node configured past the margin detects aircraft its region never asks
    for, and thin truth reads exactly like quiet airspace, so the shortfall has
    to be said out loud.
    """

    @staticmethod
    def _connect_with_range(node_id, max_range_km):
        state.connected_nodes[node_id] = {
            "status": "connected",
            "is_synthetic": False,
            "config": {"rx_lat": ATLANTA[0], "rx_lon": ATLANTA[1], "max_range_km": max_range_km},
        }

    @staticmethod
    async def _fetch():
        with patch.object(periodic, "_fetch_adsb_lol", _lol_covering_all()):
            with patch.object(periodic, "_fetch_opensky", AsyncMock(return_value=({}, set(), set(), False))):
                await periodic._fetch_external_adsb()

    @pytest.mark.asyncio
    async def test_a_range_past_the_margin_names_the_range_and_the_margin(self, caplog):
        state.connected_nodes.clear()
        self._connect_with_range("test-long-range", 300.0)

        with caplog.at_level(logging.WARNING):
            await self._fetch()

        # Both figures, or an operator cannot tell whether to fix the config or
        # raise the constant.
        assert "300" in caplog.text and "150" in caplog.text

    @pytest.mark.asyncio
    async def test_the_margin_itself_is_not_reported_as_a_shortfall(self, caplog):
        state.connected_nodes.clear()
        self._connect_with_range("test-at-margin", 150.0)
        self._connect_with_range("test-short-range", 140.0)

        with caplog.at_level(logging.WARNING):
            await self._fetch()

        assert "query margin" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_max_range_that_is_not_a_number_is_not_measured_against_it(self, caplog):
        # The bulk-detections route writes a config dict through unvalidated, so
        # this field arrives with no type guarantee either.
        state.connected_nodes.clear()
        self._connect_with_range("test-rubbish-range", "far")
        self._connect_with_range("test-bool-range", True)

        with caplog.at_level(logging.WARNING):
            await self._fetch()

        assert "query margin" not in caplog.text
