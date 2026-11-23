"""The per-address limit on the public archive reads (services/archive_rate_limit.py).

Driven through main.app as deployed: the routers' wiring, the dependency and
the client-address rule. conftest.py turns the limit off for the rest of the
suite; the `limit` fixture here turns it on with a clock the test drives. The
Data Explorer's listing and file reads are stubbed, and the tracks,
availability and detections requests carry a window the route refuses before
any store is read, so no request here touches a disk or a bucket.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import routes.archive as archive_route
from config.constants import archive_rate_limits
from main import app
from routes.openapi_documents import public_document
from services import archive_rate_limit
from services.archive_rate_limit import ArchiveRateLimiter

ARCHIVE = "/api/data/archive"
ARCHIVE_FILE = f"{ARCHIVE}/2026/09/01/node_ref=ref-a/part-000000-1.parquet"
TRACKS = "/api/data/tracks?from=2000&to=1000"
ADMITTED = 422

# Each map archive route, with a request it answers without reading a store,
# and what it answers when admitted.
ROUTES = {
    "tracks": (TRACKS, 422),
    "availability": ("/api/data/tracks/availability?from=2026-09-02&to=2026-09-01", 422),
    "detections": ("/api/data/detections?from=2000&to=1000", 422),
}


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def limit(monkeypatch, clock):
    """Turn the limit on with the environment given (unset: the defaults) and
    the test's clock, as the process would build it at its first read."""

    def _on(**env: str) -> ArchiveRateLimiter:
        for name in ("ARCHIVE_RATE_BURST", "ARCHIVE_RATE_PER_MINUTE"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        limiter = ArchiveRateLimiter.from_environment(clock=clock)
        monkeypatch.setattr(archive_rate_limit, "_limiter", limiter)
        return limiter

    return _on


@pytest.fixture(autouse=True)
def _archive_store(monkeypatch):
    """The Data Explorer's listing and file read answer from nothing."""
    monkeypatch.setattr(archive_route, "public_scope", lambda: None)
    monkeypatch.setattr(archive_route, "list_files", AsyncMock(return_value={"files": [], "count": 0, "total": 0}))
    monkeypatch.setattr(archive_route, "read_file", lambda scope, key, reader: {"key": key})


@contextmanager
def _peer(source: str, *, trusted_proxy: bool = False) -> Iterator[TestClient]:
    """A caller whose transport peer is `source`, whatever headers it sends;
    behind uvicorn's proxy-header handling trusting 127.0.0.1, as deployed.
    Not entered as a context, so the app's lifespan does not run."""
    target = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1") if trusted_proxy else app

    async def connected(scope, receive, send):
        await target({**scope, "client": (source, 12345)}, receive, send)

    client = TestClient(connected, raise_server_exceptions=False)
    try:
        yield client
    finally:
        client.close()


def test_the_31st_read_inside_ten_seconds_is_refused_with_the_wait(limit, clock):
    limit()
    clock.now = 1000.7
    with _peer("203.0.113.7") as caller:
        admitted = [caller.get(TRACKS).status_code for _ in range(30)]
        refused = caller.get(TRACKS)
    assert admitted == [ADMITTED] * 30
    assert refused.status_code == 429
    # The burst window runs 1000..1010, so 9.3 s are left, rounded up.
    assert refused.headers["Retry-After"] == "10"
    assert refused.json() == {"detail": "Too many archive requests from this address; try again in 10 s."}
    assert "203.0.113" not in refused.text


def test_another_address_keeps_its_own_budget(limit):
    limit()
    with _peer("203.0.113.7") as caller:
        for _ in range(30):
            caller.get(TRACKS)
        assert caller.get(TRACKS).status_code == 429
    with _peer("198.51.100.7") as other:
        assert other.get(TRACKS).status_code == ADMITTED


def test_behind_the_trusted_proxy_the_client_it_forwards_is_the_source(limit):
    """nginx appends the address it validated, so the rightmost hop it did not
    add is the client, whatever the client wrote to the left of it."""
    limit(ARCHIVE_RATE_BURST="1", ARCHIVE_RATE_PER_MINUTE="0")
    with _peer("127.0.0.1", trusted_proxy=True) as proxy:
        assert proxy.get(TRACKS, headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.1"}).status_code == ADMITTED
        assert proxy.get(TRACKS, headers={"X-Forwarded-For": "198.51.100.2, 203.0.113.1"}).status_code == 429
        assert proxy.get(TRACKS, headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.2"}).status_code == ADMITTED


def test_a_forwarding_header_from_an_untrusted_peer_buys_no_budget(limit):
    limit(ARCHIVE_RATE_BURST="1", ARCHIVE_RATE_PER_MINUTE="0")
    with _peer("198.51.100.9") as caller:
        statuses = []
        for suffix in (1, 2):
            spoofed = f"203.0.113.{suffix}"
            headers = {"X-Forwarded-For": spoofed, "X-Real-IP": spoofed, "CF-Connecting-IP": spoofed}
            statuses.append(caller.get(TRACKS, headers=headers).status_code)
    assert statuses == [ADMITTED, 429]


def test_with_the_burst_window_off_the_minute_still_counts(limit, clock):
    limit(ARCHIVE_RATE_BURST="0")
    with _peer("203.0.113.9") as caller:
        admitted = [caller.get(TRACKS).status_code for _ in range(120)]
        refused = caller.get(TRACKS)
    assert admitted == [ADMITTED] * 120
    assert refused.status_code == 429
    # The minute window runs 960..1020 and the clock stands at 1000.
    assert refused.headers["Retry-After"] == "20"


def test_with_both_windows_full_the_retry_after_is_the_wait_that_frees_both(limit, clock):
    limit()
    with _peer("203.0.113.10") as caller:
        admitted = []
        # 30 reads in each of the four 10 s windows from 1020, which fills the
        # minute window 1020..1080 at 120.
        for start in (1020.0, 1030.0, 1040.0, 1050.0):
            clock.now = start
            admitted += [caller.get(TRACKS).status_code for _ in range(30)]
        # Both windows are full at 1050.5. The burst window reopens at 1060, but
        # the read needs the minute window too, which reopens at 1080.
        clock.now = 1050.5
        refused = caller.get(TRACKS)
        clock.now += int(refused.headers["Retry-After"])
        retried = caller.get(TRACKS)
    assert admitted == [ADMITTED] * 120
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "30"
    assert refused.json() == {"detail": "Too many archive requests from this address; try again in 30 s."}
    assert retried.status_code == ADMITTED


def test_with_both_windows_off_nothing_is_counted(limit):
    limiter = limit(ARCHIVE_RATE_BURST="0", ARCHIVE_RATE_PER_MINUTE="0")
    assert all(limiter.admit("203.0.113.9") is None for _ in range(200))
    assert limiter.tracked_counters == 0


@pytest.mark.parametrize("raw", ["-5", "ten", "2.5"])
def test_a_negative_or_non_integer_limit_is_the_default_with_a_warning(monkeypatch, caplog, raw):
    monkeypatch.setenv("ARCHIVE_RATE_BURST", raw)
    monkeypatch.setenv("ARCHIVE_RATE_PER_MINUTE", raw)
    with caplog.at_level(logging.WARNING):
        assert archive_rate_limits() == (30, 120)
    assert len(caplog.records) == 2


def test_a_full_map_refuses_only_the_addresses_it_does_not_hold(monkeypatch, clock, caplog):
    """A caller with many addresses can fill the map; it cannot evict what is
    there, and what it is told and what is logged names nobody."""
    monkeypatch.setattr(archive_rate_limit, "_limiter", ArchiveRateLimiter(30, 120, clock=clock, max_sources=2))
    with caplog.at_level(logging.INFO), _peer("203.0.113.1") as a, _peer("203.0.113.2") as b:
        assert a.get(TRACKS).status_code == ADMITTED
        assert b.get(TRACKS).status_code == ADMITTED
        with _peer("198.51.100.3") as newcomer:
            refused = newcomer.get(TRACKS)
        assert a.get(TRACKS).status_code == ADMITTED
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "1"
    assert "198.51.100" not in refused.text
    messages = [record.getMessage() for record in caplog.records]
    assert [m for m in messages if m.startswith("archive rate limit map holds 4 live counters, at its 4 bound")]
    assert not [m for m in messages if "203.0.113" in m or "198.51.100" in m]


@pytest.mark.parametrize(("path", "admitted"), ROUTES.values(), ids=ROUTES.keys())
def test_every_map_archive_route_spends_from_the_budget(limit, path, admitted):
    limit(ARCHIVE_RATE_BURST="1", ARCHIVE_RATE_PER_MINUTE="0")
    with _peer("203.0.113.20") as caller:
        assert caller.get(path).status_code == admitted
        assert caller.get(path).status_code == 429


def test_the_map_archive_routes_share_one_budget(limit):
    limit(ARCHIVE_RATE_BURST="3", ARCHIVE_RATE_PER_MINUTE="0")
    with _peer("203.0.113.21") as caller:
        statuses = [caller.get(path).status_code for path, _ in ROUTES.values()]
        refused = caller.get(TRACKS)
    assert statuses == [admitted for _, admitted in ROUTES.values()]
    assert refused.status_code == 429


def test_the_data_explorer_reads_are_not_counted(limit):
    limit()
    with _peer("203.0.113.22") as caller:
        listings = [caller.get(ARCHIVE).status_code for _ in range(31)]
        files = [caller.get(ARCHIVE_FILE).status_code for _ in range(31)]
        tracks = caller.get(TRACKS)
    assert listings == [200] * 31
    assert files == [200] * 31
    assert tracks.status_code == ADMITTED


def test_the_reference_documents_the_refusal_on_the_map_archive_routes_only():
    paths = public_document(app.openapi(), app.description)["paths"]
    for path in ("/api/data/tracks", "/api/data/tracks/availability", "/api/data/detections"):
        refusal = paths[path]["get"]["responses"]["429"]
        assert refusal["headers"]["Retry-After"]["required"] is True, path
        assert refusal["content"]["application/json"]["schema"]["required"] == ["detail"], path
    for path in ("/api/data/archive", "/api/data/archive/{key}"):
        assert "429" not in paths[path]["get"]["responses"], path
