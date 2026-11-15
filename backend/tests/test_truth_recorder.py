"""deploy/perf-eval/truth_recorder.py, against invented registries and answers.

The recorder is the node-performance evaluation's independent denominator, so
what matters here is that its regions cover what the registry says, that every
distinct position is written once, and that rows land where the evaluation
reads them. Loaded by path: it is a stdlib-only script that runs on the host
under systemd, outside the backend package, and must stay importable that way.
"""

from __future__ import annotations

import email.message
import gzip
import importlib.util
import io
import json
import math
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "deploy" / "perf-eval" / "truth_recorder.py"
_spec = importlib.util.spec_from_file_location("truth_recorder", _PATH)
recorder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recorder)


def ms(*moment: int) -> int:
    """Epoch milliseconds of a UTC datetime given as its components."""
    return int(datetime(*moment, tzinfo=UTC).timestamp() * 1000)


def meridian_km(degrees: float) -> float:
    """Arc length along a meridian: an independent check on haversine_km for north-south offsets."""
    return 6371.0 * math.radians(degrees)


def read_rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


# ── Regions ──────────────────────────────────────────────────────────────────


def make_node(node_id: str, rx_lat: float, rx_lon: float, tx_lat=None, tx_lon=None, max_range_km=60.0) -> dict:
    return {
        "node_id": node_id,
        "rx_lat": rx_lat,
        "rx_lon": rx_lon,
        "tx_lat": tx_lat,
        "tx_lon": tx_lon,
        "max_range_km": max_range_km,
    }


def test_build_regions_clusters_nearby_nodes_and_isolates_the_far_one():
    nodes = [
        make_node("nde-bbb", 34.1, -82.0),
        make_node("nde-aaa", 34.0, -82.0),
        make_node("nde-ccc", 40.0, -75.0),
    ]
    regions = recorder.build_regions(nodes, cluster_km=60.0, margin_km=10.0, min_nm=30, max_nm=120)
    assert [r["name"] for r in regions] == ["nde-aaa+nde-bbb", "nde-ccc"]
    pair, alone = regions
    assert pair["members"] == ["nde-aaa", "nde-bbb"]
    assert (pair["lat"], pair["lon"]) == (34.05, -82.0)
    assert alone["members"] == ["nde-ccc"]
    assert (alone["lat"], alone["lon"]) == (40.0, -75.0)


def test_build_regions_radius_is_spread_plus_reach_plus_margin_in_nm():
    # Two receivers 0.1 deg apart on one meridian, both transmitters unplaced.
    nodes = [make_node("nde-aaa", 34.0, -82.0), make_node("nde-bbb", 34.1, -82.0)]
    (region,) = recorder.build_regions(nodes, cluster_km=60.0, margin_km=7.0, min_nm=30, max_nm=120)
    spread_km = meridian_km(0.05)  # each receiver's distance from the centre
    reach_km = 60.0 / 2.0 + recorder.DEFAULT_BASELINE_KM
    assert region["radius_nm"] == math.ceil((spread_km + reach_km + 7.0) / recorder.KM_PER_NM)
    assert region["radius_nm"] == 56


@pytest.mark.parametrize(
    ("min_nm", "max_nm", "expected"),
    [(30, 120, 54), (80, 120, 80), (10, 40, 40)],
    ids=["within", "floor", "ceiling"],
)
def test_build_regions_radius_is_clamped(min_nm, max_nm, expected):
    # One receiver, transmitter unplaced: 60/2 + 60 + 10 = 100 km = 53.996 nm.
    (region,) = recorder.build_regions([make_node("nde-aaa", 34.0, -82.0)], 60.0, 10.0, min_nm, max_nm)
    assert region["radius_nm"] == expected


def test_reach_km_uses_the_default_baseline_when_the_transmitter_is_unplaced():
    expected = 30.0 + recorder.DEFAULT_BASELINE_KM
    assert recorder.reach_km(make_node("nde-aaa", 34.0, -82.0)) == expected
    assert recorder.reach_km(make_node("nde-aaa", 34.0, -82.0, tx_lat=0.0, tx_lon=0.0)) == expected


def test_reach_km_uses_the_measured_baseline_capped():
    near = make_node("nde-aaa", 34.0, -82.0, tx_lat=34.5, tx_lon=-82.0)  # 0.5 deg north, 55.6 km
    assert recorder.reach_km(near) == pytest.approx(30.0 + meridian_km(0.5), rel=1e-9)
    far = make_node("nde-aaa", 34.0, -82.0, tx_lat=37.0, tx_lon=-82.0)  # 3 deg north, 334 km
    assert recorder.reach_km(far) == 30.0 + recorder.MAX_BASELINE_KM


def test_reach_km_falls_back_to_the_default_window():
    node = make_node("nde-aaa", 34.0, -82.0, max_range_km=None)
    assert recorder.reach_km(node) == recorder.DEFAULT_WINDOW_KM / 2.0 + recorder.DEFAULT_BASELINE_KM


# ── Registry ─────────────────────────────────────────────────────────────────

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
# As SQLAlchemy stores the registry's datetimes in SQLite: naive, space-separated.
RECENT = "2026-11-14 10:00:00.000000"
STALE = "2026-11-01 10:00:00.000000"


def make_registry(path: Path, configs: list[tuple], nodes: list[tuple]) -> str:
    con = sqlite3.connect(path)
    with con:
        con.execute(
            "CREATE TABLE node_configs (node_id TEXT, rx_lat REAL, rx_lon REAL, tx_lat REAL, tx_lon REAL, "
            "max_range_km REAL, superseded_at TEXT)"
        )
        con.execute("CREATE TABLE nodes (node_id TEXT PRIMARY KEY, last_seen_at TEXT, status TEXT)")
        con.executemany("INSERT INTO node_configs VALUES (?, ?, ?, ?, ?, ?, ?)", configs)
        con.executemany("INSERT INTO nodes VALUES (?, ?, ?)", nodes)
    con.close()
    return str(path)


def test_registry_nodes_returns_the_current_config_of_a_real_recent_node(tmp_path):
    configs = [
        ("nde-keep", 33.0, -84.0, 33.1, -84.1, 60.0, "2026-10-01 00:00:00.000000"),  # the row it replaced
        ("nde-keep", 34.0, -82.0, 34.5, -82.0, 59.96, None),
    ]
    copy = make_registry(tmp_path / "users.db", configs, [("nde-keep", RECENT, "active")])
    (node,) = recorder.registry_nodes(copy, recent_days=7.0, now=NOW)
    assert node["node_id"] == "nde-keep"
    assert (node["rx_lat"], node["rx_lon"], node["tx_lat"], node["tx_lon"]) == (34.0, -82.0, 34.5, -82.0)
    assert node["max_range_km"] == 59.96


@pytest.mark.parametrize(
    ("config", "node_row"),
    [
        (("synth-gvl-1", 34.0, -82.0, None, None, 60.0, None), ("synth-gvl-1", RECENT, "active")),
        (("nde-retired", 34.0, -82.0, None, None, 60.0, None), ("nde-retired", RECENT, "retired")),
        (("nde-origin", 0.0, 0.0, None, None, 60.0, None), ("nde-origin", RECENT, "active")),
        (("nde-unplaced", None, None, None, None, 60.0, None), ("nde-unplaced", RECENT, "active")),
        (("nde-stale", 34.0, -82.0, None, None, 60.0, None), ("nde-stale", STALE, "active")),
        (
            ("nde-replaced", 34.0, -82.0, None, None, 60.0, "2026-11-10 00:00:00.000000"),
            ("nde-replaced", RECENT, "active"),
        ),
    ],
    ids=["synthetic", "retired", "receiver-at-origin", "receiver-null", "seen-before-cutoff", "superseded-config"],
)
def test_registry_nodes_drops(tmp_path, config, node_row):
    copy = make_registry(tmp_path / "users.db", [config], [node_row])
    assert recorder.registry_nodes(copy, recent_days=7.0, now=NOW) == []


# ── Answers ──────────────────────────────────────────────────────────────────

NOW_MS = ms(2026, 11, 15, 13, 30)  # an answer's own build time
REGION = {"name": "nde-aaa+nde-bbb", "lat": 34.05, "lon": -82.0, "radius_nm": 56}


def aircraft(*without: str, **overrides) -> dict:
    """One aircraft as adsb.lol / adsb-service shape it, less the keys named, with the overrides."""
    a = {
        "hex": "A1B2C3",
        "lat": 33.9,
        "lon": -84.4,
        "seen_pos": 4.0,
        "flight": "DAL1 ",
        "alt_baro": 24000,
        "gs": 400,
        "track": 90,
        "mlat": ["lat"],
        "tisb": [],
        "type": "adsb_icao",
        "nic": 8,
    }
    a.update(overrides)
    for key in without:
        del a[key]
    return a


def test_aircraft_rows_parses_an_adsb_lol_shaped_answer():
    recv_s = NOW_MS / 1000.0 + 2.5  # received 2.5 s after the answer was built
    rows, count = recorder.aircraft_rows({"now": NOW_MS, "ac": [aircraft()]}, REGION, recv_s, {})
    assert count == 1
    assert rows == [
        {
            "k": "a",
            "g": "nde-aaa+nde-bbb",
            "n": NOW_MS,
            "r": NOW_MS + 2_500,
            "c": NOW_MS - 4_000,
            "h": "a1b2c3",
            "la": 33.9,
            "lo": -84.4,
            "f": "DAL1",
            "ab": 24000,
            "gs": 400,
            "tr": 90,
            "sp": 4.0,
            "nic": 8,
            "ty": "adsb_icao",
            "ml": 1,
        }
    ]


def test_aircraft_rows_flags_mlat_and_tisb_only_when_listed():
    body = {"now": NOW_MS, "ac": [aircraft(mlat=[], tisb=["lat"])]}
    (row,) = recorder.aircraft_rows(body, REGION, NOW_MS / 1000.0 + 1, {})[0]
    assert row["tb"] == 1
    assert "ml" not in row


@pytest.mark.parametrize(
    ("now_ms", "recv_ms", "base_ms"),
    [
        (NOW_MS, NOW_MS + 2_500, NOW_MS),  # received after the build: the answer's own clock
        (NOW_MS, NOW_MS - 1_500, NOW_MS - 1_500),  # a now ahead of us: bounded by receipt
        (NOW_MS, NOW_MS + 400_000, NOW_MS + 400_000),  # 400 s apart: now is garbage, receipt stands in
        (None, NOW_MS + 2_500, NOW_MS + 2_500),  # no now at all
    ],
    ids=["after", "before", "skewed", "missing"],
)
def test_aircraft_rows_capture_time_is_bounded_now_less_seen_pos(now_ms, recv_ms, base_ms):
    body = {"ac": [aircraft(seen_pos=4.0)]}
    if now_ms is not None:
        body["now"] = now_ms
    (row,) = recorder.aircraft_rows(body, REGION, recv_ms / 1000.0, {})[0]
    assert row["c"] == base_ms - 4_000
    assert row["n"] == now_ms
    assert row["r"] == recv_ms


@pytest.mark.parametrize(
    "entry",
    [
        aircraft("seen_pos"),
        aircraft(seen_pos=True),
        aircraft("lat", "lon"),
        aircraft(lat=0.0, lon=0.0),
        aircraft("hex"),
        "not an aircraft",
    ],
    ids=["no-seen_pos", "boolean-seen_pos", "no-position", "origin", "no-hex", "not-a-dict"],
)
def test_aircraft_rows_drops_rows_without_a_usable_fix_but_counts_them(entry):
    body = {"now": NOW_MS, "ac": [entry, aircraft()]}
    rows, count = recorder.aircraft_rows(body, REGION, NOW_MS / 1000.0 + 1, {})
    assert count == 2
    assert [r["h"] for r in rows] == ["a1b2c3"]


def test_aircraft_rows_writes_each_distinct_fix_once_per_region():
    last = {}
    first = {"now": NOW_MS, "ac": [aircraft(seen_pos=4.0)]}
    rows, _ = recorder.aircraft_rows(first, REGION, NOW_MS / 1000.0 + 1, last)
    assert [r["c"] for r in rows] == [NOW_MS - 4_000]
    # Six seconds on, the same fix is six seconds older: the same capture, not written again.
    repeat = {"now": NOW_MS + 6_000, "ac": [aircraft(seen_pos=10.0)]}
    assert recorder.aircraft_rows(repeat, REGION, NOW_MS / 1000.0 + 7, last) == ([], 1)
    # Seen through another region it is that region's row.
    rows, _ = recorder.aircraft_rows(repeat, dict(REGION, name="nde-ccc"), NOW_MS / 1000.0 + 7, last)
    assert [(r["g"], r["c"]) for r in rows] == [("nde-ccc", NOW_MS - 4_000)]
    # A new position is written.
    moved = {"now": NOW_MS + 12_000, "ac": [aircraft(seen_pos=0.5, lat=33.95)]}
    rows, _ = recorder.aircraft_rows(moved, REGION, NOW_MS / 1000.0 + 13, last)
    assert [(r["c"], r["la"]) for r in rows] == [(NOW_MS + 11_500, 33.95)]


# ── Hourly files ─────────────────────────────────────────────────────────────


def test_hourly_writer_files_rows_by_utc_hour_of_receipt(tmp_path):
    writer = recorder.HourlyWriter(str(tmp_path), flush_s=3600.0, flush_lines=1000)
    writer.add({"k": "p", "g": "r", "r": ms(2026, 11, 15, 13, 30)})
    writer.add({"k": "a", "g": "r", "r": ms(2026, 11, 15, 13, 59, 59), "h": "a1b2c3"})
    writer.add({"k": "p", "g": "r", "r": ms(2026, 11, 15, 14, 0)})
    writer.add({"k": "p", "g": "r", "r": ms(2026, 11, 16, 0, 5)})
    assert not list(tmp_path.rglob("*.jsonl.gz"))  # buffered until flushed
    writer.flush()
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.jsonl.gz")) == [
        "2026-11-15/13.jsonl.gz",
        "2026-11-15/14.jsonl.gz",
        "2026-11-16/00.jsonl.gz",
    ]
    assert [r["r"] for r in read_rows(tmp_path / "2026-11-15" / "13.jsonl.gz")] == [
        ms(2026, 11, 15, 13, 30),
        ms(2026, 11, 15, 13, 59, 59),
    ]
    assert writer.written == 4


def test_hourly_writer_appends_gzip_members_that_read_back_as_one_stream(tmp_path):
    writer = recorder.HourlyWriter(str(tmp_path), flush_s=3600.0, flush_lines=1000)
    stamp = ms(2026, 11, 15, 13, 30)
    writer.add({"k": "p", "g": "r", "r": stamp, "st": 200})
    writer.flush()
    writer.add({"k": "a", "g": "r", "r": stamp + 10, "h": "a1b2c3"})
    writer.add({"k": "a", "g": "r", "r": stamp + 20, "h": "d4e5f6"})
    writer.flush()
    rows = read_rows(tmp_path / "2026-11-15" / "13.jsonl.gz")
    assert [r["r"] for r in rows] == [stamp, stamp + 10, stamp + 20]
    assert [r.get("h") for r in rows] == [None, "a1b2c3", "d4e5f6"]
    assert writer.written == 3


def test_hourly_writer_add_flushes_once_flush_lines_is_reached(tmp_path):
    writer = recorder.HourlyWriter(str(tmp_path), flush_s=3600.0, flush_lines=3)
    stamp = ms(2026, 11, 15, 13, 30)
    path = tmp_path / "2026-11-15" / "13.jsonl.gz"
    writer.add({"k": "a", "g": "r", "r": stamp, "h": "a1b2c3"})
    writer.add({"k": "a", "g": "r", "r": stamp + 1, "h": "a1b2c3"})
    assert not path.exists()
    assert writer.pending == 2
    writer.add({"k": "a", "g": "r", "r": stamp + 2, "h": "a1b2c3"})
    assert [r["r"] for r in read_rows(path)] == [stamp, stamp + 1, stamp + 2]
    assert writer.pending == 0
    assert writer.buffer == {}


# ── Fetching ─────────────────────────────────────────────────────────────────

BASE_URL = "https://adsb.example.test"


def http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    for name, value in (headers or {}).items():
        hdrs[name] = value
    return urllib.error.HTTPError(f"{BASE_URL}/v2/point/34.05/-82.0/56", code, "nope", hdrs, None)


def test_fetch_point_requests_the_region_disc_and_returns_body_receipt_and_duration(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["user_agent"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        # What urlopen returns is read inside a `with`: BytesIO is a context manager with read().
        return io.BytesIO(b'{"now": 1794700000000, "ac": []}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    before = time.time()
    body, recv_s, took = recorder.fetch_point(BASE_URL, REGION, 8.0)
    assert body == {"now": 1794700000000, "ac": []}
    assert before <= recv_s <= time.time()
    assert 0.0 <= took < 5.0
    assert seen == {"url": f"{BASE_URL}/v2/point/34.05/-82.0/56", "user_agent": recorder.USER_AGENT, "timeout": 8.0}


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "7"}, 7.0),
        ({"Retry-After": "0.5"}, 0.5),
        ({}, 5.0),
        ({"Retry-After": "Sun, 15 Nov 2026 12:00:07 GMT"}, 5.0),
        ({"Retry-After": "-3"}, 5.0),
        ({"Retry-After": "inf"}, 5.0),
    ],
    ids=["seconds", "fraction", "absent", "http-date", "negative", "infinite"],
)
def test_fetch_point_turns_a_429_into_rate_limited(monkeypatch, headers, expected):
    def limited(*_args, **_kwargs):
        raise http_error(429, headers)

    monkeypatch.setattr(urllib.request, "urlopen", limited)
    with pytest.raises(recorder.RateLimited) as caught:
        recorder.fetch_point(BASE_URL, REGION, 8.0)
    assert caught.value.retry_after_s == expected


def test_fetch_point_reraises_other_http_errors(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise http_error(503)

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    with pytest.raises(urllib.error.HTTPError) as caught:
        recorder.fetch_point(BASE_URL, REGION, 8.0)
    assert caught.value.code == 503


# ── Polling ──────────────────────────────────────────────────────────────────


def make_recorder(tmp_path: Path) -> recorder.Recorder:
    args = recorder.build_parser().parse_args(
        ["--root", str(tmp_path), "--base-url", BASE_URL, "--flush-s", "3600", "--max-retry-after-s", "60"]
    )
    return recorder.Recorder(args)


def written_rows(tmp_path: Path) -> list[dict]:
    rows = []
    for path in sorted((tmp_path / "truth" / "adsbhub").rglob("*.jsonl.gz")):
        rows.extend(read_rows(path))
    return rows


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (urllib.error.URLError("connection refused"), 0),
        (urllib.error.URLError("x" * 300), 0),
        (http_error(503), 503),
        (TimeoutError("timed out"), 0),
        (json.JSONDecodeError("Expecting value", "<html>", 0), 0),
    ],
    ids=["unreachable", "long-reason", "http-503", "timeout", "not-json"],
)
def test_poll_records_a_failed_request_and_moves_on(tmp_path, monkeypatch, exc, status):
    rec = make_recorder(tmp_path)

    def failing(*_args):
        raise exc

    monkeypatch.setattr(recorder, "fetch_point", failing)
    before = int(time.time() * 1000)
    assert rec.poll(REGION) == 0.0
    rec.writer.flush()
    (row,) = written_rows(tmp_path)
    assert (row["k"], row["g"], row["st"]) == ("p", "nde-aaa+nde-bbb", status)
    assert row["err"] == str(exc)[:200]
    assert before <= row["r"] <= int(time.time() * 1000)
    assert "na" not in row
    assert rec.counters == {"cycles": 0, "requests": 1, "ok": 0, "failed": 1, "rate_limited": 0, "rows": 0}
    assert rec.last_ok_by_region == {}


@pytest.mark.parametrize(("retry_after_s", "expected"), [(7.0, 7.0), (900.0, 60.0)], ids=["honoured", "capped"])
def test_poll_returns_the_capped_retry_after_on_a_429(tmp_path, monkeypatch, retry_after_s, expected):
    rec = make_recorder(tmp_path)

    def limited(*_args):
        raise recorder.RateLimited(retry_after_s)

    monkeypatch.setattr(recorder, "fetch_point", limited)
    assert rec.poll(REGION) == expected
    rec.writer.flush()
    (row,) = written_rows(tmp_path)
    assert (row["k"], row["g"], row["st"]) == ("p", "nde-aaa+nde-bbb", 429)
    assert row["err"] == f"429, retry after {retry_after_s:.0f}s"
    assert rec.counters == {"cycles": 0, "requests": 1, "ok": 0, "failed": 0, "rate_limited": 1, "rows": 0}
    assert rec.last_ok_by_region == {}


def test_poll_writes_the_poll_row_then_one_row_per_aircraft(tmp_path, monkeypatch):
    rec = make_recorder(tmp_path)
    recv_s = NOW_MS / 1000.0 + 2.5
    body = {
        "now": NOW_MS,
        "ac": [
            aircraft(),
            aircraft(hex="D4E5F6", lat=34.2, lon=-83.1, flight="SWA22"),
            aircraft("seen_pos", hex="000001"),  # counted in the answer, never a row
        ],
    }

    def served(base_url, region, timeout_s):
        assert (base_url, region, timeout_s) == (BASE_URL, REGION, 8.0)
        return body, recv_s, 0.1234

    monkeypatch.setattr(recorder, "fetch_point", served)
    assert rec.poll(REGION) == 0.0
    rec.writer.flush()
    files = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*.jsonl.gz"))
    assert [str(p) for p in files] == ["truth/adsbhub/2026-11-15/13.jsonl.gz"]
    rows = written_rows(tmp_path)
    assert [r["k"] for r in rows] == ["p", "a", "a"]
    assert rows[0] == {
        "k": "p",
        "g": "nde-aaa+nde-bbb",
        "n": NOW_MS,
        "r": NOW_MS + 2_500,
        "st": 200,
        "na": 3,
        "nw": 2,
        "d": 123,
        "lat": 34.05,
        "lon": -82.0,
        "nm": 56,
    }
    assert [(r["h"], r["f"]) for r in rows[1:]] == [("a1b2c3", "DAL1"), ("d4e5f6", "SWA22")]
    assert rec.last_ok_by_region == {"nde-aaa+nde-bbb": NOW_MS + 2_500}
    assert rec.counters == {"cycles": 0, "requests": 1, "ok": 1, "failed": 0, "rate_limited": 0, "rows": 2}


def test_poll_does_not_rewrite_a_fix_it_has_already_written(tmp_path, monkeypatch):
    rec = make_recorder(tmp_path)
    answers = iter(
        [
            ({"now": NOW_MS, "ac": [aircraft(seen_pos=4.0)]}, NOW_MS / 1000.0 + 1, 0.1),
            ({"now": NOW_MS + 6_000, "ac": [aircraft(seen_pos=10.0)]}, NOW_MS / 1000.0 + 7, 0.1),
        ]
    )
    monkeypatch.setattr(recorder, "fetch_point", lambda *_args: next(answers))
    assert rec.poll(REGION) == 0.0
    assert rec.poll(REGION) == 0.0
    rec.writer.flush()
    assert [(r["k"], r.get("nw")) for r in written_rows(tmp_path)] == [("p", 1), ("a", None), ("p", 0)]
    assert rec.counters["rows"] == 1
    assert rec.last_ok_by_region == {"nde-aaa+nde-bbb": NOW_MS + 7_000}
