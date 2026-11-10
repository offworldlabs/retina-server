"""The stored tower match: callsign forms, statuses, and what a failed lookup leaves alone."""

import asyncio
import errno
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.nodes import Node, NodeConfig
from services import tower_match
from services.node_auth import mint_node_ref
from services.tasks.tower_match_refresh import run_pass
from services.tower_match import load, lookup, refresh, resolve

_URL = "http://towers.example.com"
_TODAY = "2026-03-09"
_SITE_A = (10.1111, -30.2222)
_SITE_B = (12.3333, -32.4444)
_SITE_C = (14.5555, -34.6666)
_STORED_KEY = "QAAA|195.000|10.1111|-30.2222"
_STORED = {"callsign": "QAAA-TV", "frequency_mhz": 195.0, "eirp_dbm": 81.2, "status": "matched", "date": "2026-02-01"}


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(tower_match, "_now", lambda: datetime(2026, 3, 9, 12, 0, tzinfo=UTC))


def _config(callsign, fc_mhz, site=_SITE_A, **over):
    row = {"node_id": "node-one", "tx_callsign": callsign, "fc_hz": fc_mhz * 1e6, "tx_lat": site[0], "tx_lon": site[1]}
    row.update(rx_lat=50.5, rx_lon=-60.5, version=1, superseded_at=None)
    row.update(over)
    return row


def _record(callsign, frequency_mhz, **over):
    row = {"callsign": callsign, "frequency_mhz": frequency_mhz, "band": "UHF", "distance_km": 2.0, "eirp_dbm": 81.2}
    row.update(licence_type="Broadcasting", licence_subtype="Television", measured=False, shared_callsigns=[])
    row.update(over)
    return row


def _answer(*records):
    return httpx.Response(200, json={"towers": list(records), "query": {"radius_km": 30}, "count": len(records)})


def _refresh(configs, handler, path):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return refresh(configs, url=_URL, path=path, session=client, interval_s=0)


def _matched(callsign, frequency_mhz, eirp_dbm=81.2):
    return {
        "callsign": callsign,
        "frequency_mhz": frequency_mhz,
        "eirp_dbm": eirp_dbm,
        "status": "matched",
        "date": _TODAY,
    }


def _nothing(status):
    return {"callsign": None, "frequency_mhz": None, "eirp_dbm": None, "status": status, "date": _TODAY}


def _seeded(tmp_path, entry=_STORED):
    path = tmp_path / "tower_match.json"
    path.write_text(json.dumps({"refreshed_at": "2026-02-01T03:00:00+00:00", "towers": {_STORED_KEY: entry}}))
    return path


def test_callsign_forms_meet_with_one_request_per_tower_position_and_no_receiver_position(tmp_path):
    sent = []

    def handler(request):
        sent.append((dict(request.url.params), request.extensions["timeout"]["read"]))
        return _answer(
            _record("", 195.0, eirp_dbm=10.0),
            _record("QAAA", 195.0, licence_type="", eirp_dbm=11.0),
            _record("QAAA", 195.0, measured=True, eirp_dbm=12.0),
            _record("QAAA-TV", 195.0),
            _record("QBBB", 521.0),
            _record("Q99AA-LD", 533.0),
            _record("QCCC-TV", 569.0),
            _record("QEEE-TV", 177.0, shared_callsigns=["QDDD-CD"]),
        )

    configs = [
        _config("QAAA", 195.0),
        _config("QBBB-TV", 521.0),
        _config("Q99AA-D", 533.0),
        _config("QCCC Some Place RF27", 569.0, _SITE_B),
        _config("QDDD", 177.0, _SITE_B),
    ]
    no_callsign = _config(None, 195.0, _SITE_C)
    path = tmp_path / "coverage_data" / "tower_match.json"

    doc = _refresh([*configs, no_callsign], handler, path)

    assert [lookup(doc, config) for config in configs] == [
        _matched("QAAA-TV", 195.0),
        _matched("QBBB", 521.0),
        _matched("Q99AA-LD", 533.0),
        _matched("QCCC-TV", 569.0),
        _matched("QEEE-TV", 177.0),
    ]
    assert lookup(doc, no_callsign) == _nothing("not_found")
    assert sent == [
        ({"lat": "10.1111", "lon": "-30.2222", "radius_km": "30", "limit": "200"}, 120.0),
        ({"lat": "12.3333", "lon": "-32.4444", "radius_km": "30", "limit": "200"}, 120.0),
    ]
    assert doc["refreshed_at"] == "2026-03-09T12:00:00+00:00"
    assert load(path) == doc


def test_a_licence_on_another_frequency_gives_no_power_and_the_tolerance_edge_still_matches(tmp_path):
    def handler(request):
        return _answer(_record("QAAA-TV", 213.0), _record("QAAA-TV", 201.0), _record("QBBB", 524.5))

    elsewhere, at_the_edge = _config("QAAA", 195.0), _config("QBBB", 521.0)

    doc = _refresh([elsewhere, at_the_edge], handler, tmp_path / "tower_match.json")

    assert lookup(doc, elsewhere) == {
        "callsign": "QAAA-TV",
        "frequency_mhz": 201.0,
        "eirp_dbm": None,
        "status": "frequency_mismatch",
        "date": _TODAY,
    }
    assert lookup(doc, at_the_edge) == _matched("QBBB", 524.5)


def test_a_string_422_is_a_region_refusal_and_a_validation_422_is_a_failure(tmp_path):
    details = {
        "10.1111": "No tower data within 30 km of this location (coverage: US, CA, AU). Try a larger search radius.",
        "12.3333": "not in a supported region (US, CA, AU)",
        "14.5555": [{"type": "less_than_equal", "loc": ["query", "lat"], "msg": "Input should be at most 90"}],
    }

    def handler(request):
        return httpx.Response(422, json={"detail": details[request.url.params["lat"]]})

    checked_in, deployed = _config("QAAA", 195.0, _SITE_A), _config("QBBB", 195.0, _SITE_B)
    invalid = _config("QCCC", 195.0, _SITE_C)

    doc = _refresh([checked_in, deployed, invalid], handler, tmp_path / "tower_match.json")

    assert lookup(doc, checked_in) == _nothing("unsupported_region")
    assert lookup(doc, deployed) == _nothing("unsupported_region")
    assert lookup(doc, invalid) is None


def test_a_timeout_keeps_stored_entries_and_three_failed_positions_stop_the_asking(tmp_path):
    path = _seeded(tmp_path)
    asked = []

    def handler(request):
        asked.append(request.url.params["lat"])
        raise httpx.TimeoutException("no answer", request=request)

    stored = _config("QAAA", 195.0)
    unknown = [_config("QBBB", 195.0, (lat, -30.5)) for lat in (20.5, 21.5, 22.5, 23.5)]

    doc = _refresh([stored, *unknown], handler, path)

    assert lookup(doc, stored) == _STORED
    assert [lookup(doc, config) for config in unknown] == [None, None, None, None]
    assert asked == ["10.1111", "20.5000", "21.5000"]
    assert load(path)["towers"] == {_STORED_KEY: _STORED}


def test_an_answer_without_a_matched_tower_keeps_its_entry_and_date(tmp_path):
    config = _config("QAAA", 195.0)

    doc = _refresh([config], lambda request: _answer(_record("QZZZ", 195.0)), _seeded(tmp_path))

    assert lookup(doc, config) == _STORED


def test_an_answer_without_its_licence_keeps_a_frequency_mismatch(tmp_path):
    config = _config("QAAA", 195.0)
    stored = {
        "callsign": "QAAA-TV",
        "frequency_mhz": 201.0,
        "eirp_dbm": None,
        "status": "frequency_mismatch",
        "date": "2026-02-01",
    }

    doc = _refresh([config], lambda request: _answer(_record("QZZZ", 195.0)), _seeded(tmp_path, stored))

    assert lookup(doc, config) == stored


def test_a_tower_change_leaves_each_config_row_with_its_own_tower(tmp_path):
    path = tmp_path / "tower_match.json"
    old = _config("QAAA", 195.0, _SITE_A, version=1, superseded_at="2026-03-01T00:00:00+00:00")
    new = _config("QBBB", 521.0, _SITE_B, version=2)

    _refresh([old], lambda request: _answer(_record("QAAA", 195.0)), path)
    doc = _refresh([new], lambda request: httpx.Response(503), path)

    assert lookup(doc, new) is None
    assert lookup(doc, old) == _matched("QAAA", 195.0)

    doc = _refresh([new], lambda request: _answer(_record("QBBB", 521.0, eirp_dbm=77.7)), path)

    assert lookup(doc, new) == _matched("QBBB", 521.0, 77.7)
    assert lookup(doc, old) == _matched("QAAA", 195.0)


def test_the_routes_default_power_keeps_the_match_without_a_power(tmp_path):
    def handler(request):
        return _answer(
            _record("QAAA", 98.1, band="FM", eirp_dbm=50.0),
            _record("QBBB", 521.0, eirp_dbm=60.0),
            _record("QCCC", 533.0, eirp_dbm=50.0),
        )

    fm, tv, real_power = _config("QAAA", 98.1), _config("QBBB", 521.0), _config("QCCC", 533.0)

    doc = _refresh([fm, tv, real_power], handler, tmp_path / "tower_match.json")

    assert lookup(doc, fm) == _matched("QAAA", 98.1, None)
    assert lookup(doc, tv) == _matched("QBBB", 521.0, None)
    assert lookup(doc, real_power) == _matched("QCCC", 533.0, 50.0)


def test_a_failed_read_of_the_stored_file_asks_nothing_and_drops_nothing(tmp_path, monkeypatch):
    path = _seeded(tmp_path)
    before = path.read_bytes()
    asked = []

    def handler(request):
        asked.append(request)
        return _answer(_record("QAAA", 195.0))

    def unreadable(*_args, **_kwargs):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(tower_match, "open", unreadable, raising=False)

    with pytest.raises(OSError, match="Input/output error"):
        _refresh([_config("QAAA", 195.0)], handler, path)

    assert asked == []
    assert path.read_bytes() == before


def _resolve(handler):
    sent = []
    transport = httpx.MockTransport(lambda request: sent.append(request) or handler(request))
    with httpx.Client(transport=transport) as client:
        return resolve(client, _URL, _config("QAAA", 195.0), timeout_s=7.5), sent


def _resolved(status, callsign=None, eirp_dbm=None, antenna_height_m=None, band=None, polarisation=None):
    record = {"callsign": callsign, "eirp_dbm": eirp_dbm, "antenna_height_m": antenna_height_m, "band": band}
    return {**record, "polarisation": polarisation, "status": status}


def test_the_lookup_by_callsign_sends_only_the_tower_and_its_match_becomes_the_record():
    tower = {"callsign": "QAAA-TV", "name": "Invented Hill", "frequency_mhz": 195.0, "band": "VHF", "latitude": 10.12}
    tower.update(longitude=-30.23, eirp_dbm=79.7, antenna_height_m=481.0, polarisation="horizontal", distance_km=0.8)
    query = {"callsign": "QAAA", "frequency_mhz": 195.0, "latitude": 10.1111, "longitude": -30.2222, "radius_km": 30}
    answer = {"status": "matched", "tower": {**tower, "licence_type": "Broadcasting"}, "query": query}

    record, sent = _resolve(lambda request: httpx.Response(200, json=answer))

    assert record == _resolved("matched", "QAAA-TV", 79.7, 481.0, "VHF", "horizontal")
    params = [("callsign", "QAAA"), ("frequency_mhz", "195.0"), ("lat", "10.1111"), ("lon", "-30.2222")]
    assert [(r.url.path, r.url.params.multi_items(), r.extensions["timeout"]["read"]) for r in sent] == [
        ("/api/towers/lookup", params, 7.5)
    ]


def test_a_tower_finder_without_the_lookup_is_matched_by_a_radius_search_around_the_tower():
    def handler(request):
        if request.url.path == "/api/towers/lookup":
            return httpx.Response(404, json={"detail": "Not Found"})
        return _answer(_record("QAAA-TV", 195.0, band="VHF", antenna_height_m=305.0, polarisation="vertical"))

    record, sent = _resolve(handler)

    assert record == _resolved("matched", "QAAA-TV", 81.2, 305.0, "VHF")
    assert [r.url.path for r in sent] == ["/api/towers/lookup", "/api/towers"]
    assert dict(sent[1].url.params) == {"lat": "10.1111", "lon": "-30.2222", "radius_km": "30", "limit": "200"}


def test_a_lookup_refused_for_its_region_gives_the_unsupported_region_record():
    record, _ = _resolve(lambda request: httpx.Response(422, json={"detail": "not in a supported region (US, CA, AU)"}))

    assert record == _resolved("unsupported_region")


_NOW = datetime(2026, 3, 9, 12, 0, tzinfo=UTC)
_ROW = {"tx_callsign": "QAAA", "fc_hz": 195e6, "tx_lat": 10.1111, "tx_lon": -30.2222, "rx_lat": 10.3, "rx_lon": -30.4}
_ROW.update(fs_hz=2e6, max_range_km=50.0, cpi_s=0.5, delay_tolerance_us=6.67, doppler_tolerance_hz=5.0)
_QUERY = {"callsign": "QAAA", "frequency_mhz": 195.0, "latitude": 10.1111, "longitude": -30.2222}
_QUERY.update(radius_km=30, source="us")
_TOWER = {"callsign": "QAAA-TV", "name": "Invented Hill", "frequency_mhz": 195.0, "band": "VHF", "latitude": 10.1111}
_TOWER.update(longitude=-30.2222, eirp_dbm=79.7, antenna_height_m=481.0, polarisation="horizontal", distance_km=0.0)
_LOOKED_UP = {"status": "matched", "tower": {**_TOWER, "licence_type": "Broadcasting"}, "query": _QUERY}
_MATCH = ("QAAA-TV", 79.7, 481.0, "VHF", "horizontal", "matched")
_UNMATCHED = (None,) * 7
_COLUMNS = ("tower_callsign", "tower_eirp_dbm", "tower_antenna_height_m", "tower_band", "tower_polarisation")
_COLUMNS += ("tower_match_status", "tower_matched_at")


async def _refresh_pass(node_session, status, body, **stored):
    """One pass over a config row per node id, stored with the seven given; the seven after it, and the requests."""
    for node_id, seven in stored.items():
        node_session.add(Node(node_id=node_id, node_ref=mint_node_ref(), board_model="raspberrypi5-4gb"))
        node_session.add(NodeConfig(node_id=node_id, version=1, **_ROW, **dict(zip(_COLUMNS, seven, strict=True))))
    await node_session.commit()
    sent = []
    maker = async_sessionmaker(node_session.bind, expire_on_commit=False)
    transport = httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(status, json=body))
    with httpx.Client(transport=transport) as client:
        assert await run_pass(asyncio.to_thread, maker, client, _URL, _NOW) is True
    async with maker() as session:
        rows = await session.scalars(select(NodeConfig))
        return {row.node_id: tuple(getattr(row, column) for column in _COLUMNS) for row in rows}, sent


async def test_a_pass_fills_an_unmatched_row_and_leaves_a_fresh_match_unasked(node_session):
    yesterday = _NOW - timedelta(days=1)

    seven, sent = await _refresh_pass(
        node_session, 200, _LOOKED_UP, ret0a1b2c3d=_UNMATCHED, ret0badcafe=(*_MATCH, yesterday)
    )

    assert seven == {"ret0a1b2c3d": (*_MATCH, _NOW), "ret0badcafe": (*_MATCH, yesterday)}
    assert [r.url.path for r in sent] == ["/api/towers/lookup"]


async def test_a_failed_lookup_leaves_the_row_as_it_was(node_session):
    seven, _ = await _refresh_pass(node_session, 500, {"detail": "Internal Server Error"}, ret0a1b2c3d=_UNMATCHED)

    assert seven == {"ret0a1b2c3d": _UNMATCHED}


async def test_an_unchanged_answer_for_an_old_match_moves_only_its_time(node_session):
    seven, _ = await _refresh_pass(node_session, 200, _LOOKED_UP, ret0a1b2c3d=(*_MATCH, _NOW - timedelta(days=8)))

    assert seven == {"ret0a1b2c3d": (*_MATCH, _NOW)}


async def test_an_answer_without_the_matched_tower_keeps_the_match(node_session):
    not_found = {"status": "not_found", "tower": None, "query": _QUERY}

    seven, _ = await _refresh_pass(node_session, 200, not_found, ret0a1b2c3d=(*_MATCH, _NOW - timedelta(days=8)))

    assert seven == {"ret0a1b2c3d": (*_MATCH, _NOW)}
