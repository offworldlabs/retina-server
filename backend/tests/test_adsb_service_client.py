"""The adsb-service client: point queries, capture times and the rate-limit signal."""

import httpx
import pytest

from clients import adsb_service
from clients.adsb_service import AdsbServiceClient, RateLimited, parse_point_response

_NOW_MS = 1_790_000_000_000


def _row(**over):
    row = {"hex": "A1B2C3", "flight": "DAL123  ", "lat": 34.1, "lon": -84.2, "alt_baro": 35000, "gs": 450.0}
    row.update(track=90.0, seen_pos=4.0)
    row.update(over)
    return row


def test_capture_time_is_the_servers_now_less_seen_pos():
    rows = parse_point_response({"now": _NOW_MS, "ac": [_row(seen_pos=7.5)]}, recv_s=_NOW_MS / 1000 + 0.6)
    assert rows[0]["captured_ms"] == _NOW_MS - 7500


def test_an_answer_is_never_dated_after_its_receipt():
    # A server clock 30 s fast would otherwise hand calibration a fix from our future.
    recv_s = _NOW_MS / 1000
    rows = parse_point_response({"now": _NOW_MS + 30_000, "ac": [_row(seen_pos=1.0)]}, recv_s=recv_s)
    assert rows[0]["captured_ms"] == _NOW_MS - 1000


def test_missing_now_dates_rows_from_receipt():
    rows = parse_point_response({"ac": [_row(seen_pos=3.0)]}, recv_s=_NOW_MS / 1000)
    assert rows[0]["captured_ms"] == _NOW_MS - 3000


def test_a_row_without_seen_pos_is_dropped_rather_than_dated_from_the_poll():
    assert parse_point_response({"now": _NOW_MS, "ac": [_row(seen_pos=None)]}, recv_s=_NOW_MS / 1000) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"lat": None},
        {"lon": float("nan")},
        {"lat": 91.0},
        {"hex": "obj-001"},
        {"hex": ""},
        {"seen_pos": "stale"},
    ],
)
def test_unusable_rows_are_dropped(bad):
    assert parse_point_response({"now": _NOW_MS, "ac": [_row(**bad)]}, recv_s=_NOW_MS / 1000) == []


def test_rows_carry_a_normalised_hex_and_the_feed_fields():
    (row,) = parse_point_response({"now": _NOW_MS, "ac": [_row()]}, recv_s=_NOW_MS / 1000)
    assert row["hex"] == "a1b2c3"
    assert row["flight"] == "DAL123"
    assert (row["lat"], row["lon"], row["alt_baro"], row["gs"], row["track"]) == (34.1, -84.2, 35000, 450.0, 90.0)


@pytest.mark.parametrize("alt", ["ground", 0, -25])
def test_an_aircraft_on_the_ground_is_no_candidate(alt):
    # adsb.lol spells it "ground"; adsb.retina.fm sends a numeric 0.
    assert parse_point_response({"now": _NOW_MS, "ac": [_row(alt_baro=alt)]}, recv_s=_NOW_MS / 1000) == []


@pytest.mark.parametrize("now", [_NOW_MS / 1000, True, _NOW_MS - 3_600_000])
def test_a_now_far_behind_receipt_is_distrusted_and_receipt_used(now):
    # A `now` in seconds, or a bool, would otherwise date every row to 1970.
    rows = parse_point_response({"now": now, "ac": [_row(seen_pos=2.0)]}, recv_s=_NOW_MS / 1000)
    assert rows[0]["captured_ms"] == _NOW_MS - 2000


def test_a_malformed_body_yields_nothing():
    assert parse_point_response({"ac": None}, recv_s=0.0) == []
    assert parse_point_response({"ac": ["not a row"]}, recv_s=0.0) == []


def _client(handler) -> AdsbServiceClient:
    return AdsbServiceClient(transport=httpx.MockTransport(handler))


async def test_fetch_point_asks_the_point_endpoint_and_parses_the_answer():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"now": _NOW_MS, "ac": [_row()]})

    client = _client(handler)
    try:
        rows = await client.fetch_point(34.3634, -83.4981, 144)
    finally:
        await client.aclose()
    assert [r["hex"] for r in rows] == ["a1b2c3"]
    assert seen[0].url == httpx.URL(f"{adsb_service.BASE_URL}/v2/point/34.3634/-83.4981/144")
    assert seen[0].headers["User-Agent"].startswith("retina-server/")


async def test_a_429_raises_rate_limited_carrying_retry_after():
    client = _client(lambda request: httpx.Response(429, headers={"Retry-After": "7"}))
    try:
        with pytest.raises(RateLimited) as exc:
            await client.fetch_point(34.0, -84.0, 100)
    finally:
        await client.aclose()
    assert exc.value.retry_after_s == 7.0


async def test_a_429_without_a_usable_retry_after_waits_the_default():
    client = _client(lambda request: httpx.Response(429, headers={"Retry-After": "soon"}))
    try:
        with pytest.raises(RateLimited) as exc:
            await client.fetch_point(34.0, -84.0, 100)
    finally:
        await client.aclose()
    assert exc.value.retry_after_s == adsb_service.DEFAULT_RETRY_AFTER_S


async def test_any_other_error_status_raises():
    client = _client(lambda request: httpx.Response(503))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch_point(34.0, -84.0, 100)
    finally:
        await client.aclose()
