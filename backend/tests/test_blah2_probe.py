"""The probe that decides whether an operator's URL is a live stock blah2."""

import asyncio
import copy
import hashlib
import json
import time

import pytest

from services import blah2_probe, polled_endpoint
from services.blah2_probe import (
    CONFIG_PATH,
    CONTACT_ROUTE,
    DETECTION_PATH,
    Blah2Refusal,
    Site,
    config_fingerprint,
    freshness_gap_s,
    parse_config,
    parse_detection,
    probe_blah2,
)
from services.polled_endpoint import EndpointRefused, Refusal, parse_endpoint
from tests.radar_stub import StubServer, only_loopback, resolve_to_loopback

# /api/config as stock blah2 serves it: the whole YAML, rendered by js-yaml.
# The sites are invented and sit in open ocean.
STOCK = {
    "capture": {"fs": 2000000, "fc": 204640000, "device": {"type": "RspDuo", "agcSetPoint": -20}},
    "process": {"data": {"cpi": 0.75, "buffer": 2, "overlap": 0}},
    "network": {"ip": "0.0.0.0", "ports": {"api": 3000, "detection": 3002}},
    "truth": {"adsb": {"enabled": True, "tar1090": "adsb.example.net", "adsb2dd": "adsb2dd.example.net"}},
    "location": {
        "rx": {"latitude": 10.5, "longitude": -30.25, "altitude": 12, "name": "Receiver"},
        "tx": {"latitude": 10.75, "longitude": -30.5, "altitude": 300, "name": "Transmitter"},
    },
    "save": {"iq": True, "path": "/blah2/save/"},
}

ADELAIDE = {
    "rx": {"latitude": -34.9286, "longitude": 138.5999, "altitude": 50, "name": "Adelaide"},
    "tx": {"latitude": -34.981, "longitude": 138.7081, "altitude": 750, "name": "Mount Lofty"},
}


def _config(**location) -> dict:
    config = copy.deepcopy(STOCK)
    config["location"].update(copy.deepcopy(location))
    return config


_DELETE = object()


def _set(config: dict, path: tuple[str, ...], value) -> dict:
    config = copy.deepcopy(config)
    node = config
    for key in path[:-1]:
        node = node[key]
    if value is _DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return config


def _frame(timestamp_ms: int, n: int = 2) -> bytes:
    return json.dumps(
        {"timestamp": timestamp_ms, "delay": [12.5] * n, "doppler": [-40.0] * n, "snr": [14.2] * n}
    ).encode()


def _refusal_of(fn, *args) -> EndpointRefused:
    with pytest.raises(EndpointRefused) as info:
        fn(*args)
    return info.value


# ── /api/config ───────────────────────────────────────────────────────────────


def test_stock_config_parses():
    config = parse_config(STOCK)
    assert config.rx == Site(latitude=10.5, longitude=-30.25, altitude_m=12.0, name="Receiver")
    assert config.tx == Site(latitude=10.75, longitude=-30.5, altitude_m=300.0, name="Transmitter")
    assert config.fc_hz == 204640000.0
    assert config.fs_hz == 2000000.0


def test_fs_is_optional():
    assert parse_config(_set(STOCK, ("capture", "fs"), _DELETE)).fs_hz is None


def test_a_site_name_is_optional():
    assert parse_config(_set(STOCK, ("location", "rx", "name"), _DELETE)).rx.name is None


def test_the_blah2_arm_shape_is_sent_to_the_node_route():
    refusal = _refusal_of(parse_config, {"truth": {"adsb": {"enabled": True}}})
    assert refusal.code == Blah2Refusal.BLAH2_ARM
    assert "node" in refusal.message


@pytest.mark.parametrize(
    "config",
    [
        [],
        "blah2",
        {},
        {"hello": "world"},
        _set(STOCK, ("location",), _DELETE),
        _set(STOCK, ("location", "rx"), "here"),
        _set(STOCK, ("location", "rx", "latitude"), 91),
        _set(STOCK, ("location", "rx", "latitude"), "10.5"),
        _set(STOCK, ("location", "rx", "latitude"), True),
        _set(STOCK, ("location", "rx", "latitude"), float("nan")),
        _set(STOCK, ("location", "tx", "longitude"), -181),
        _set(STOCK, ("location", "tx", "longitude"), _DELETE),
        _set(STOCK, ("location", "tx", "altitude"), _DELETE),
        _set(STOCK, ("location", "tx", "altitude"), float("inf")),
        _set(STOCK, ("capture",), _DELETE),
        _set(STOCK, ("capture", "fc"), _DELETE),
        _set(STOCK, ("capture", "fc"), 0),
        _set(STOCK, ("capture", "fc"), -1e6),
        _set(STOCK, ("capture", "fc"), "204640000"),
        _set(STOCK, ("capture", "fc"), 10**400),
    ],
)
def test_configs_that_are_not_blah2_are_refused(config):
    assert _refusal_of(parse_config, config).code == Blah2Refusal.NOT_BLAH2


def test_the_shipped_adelaide_example_is_refused():
    refusal = _refusal_of(parse_config, _config(**ADELAIDE))
    assert refusal.code == Blah2Refusal.EXAMPLE_CONFIG
    assert "example configuration" in refusal.message
    assert "blah2 author" in refusal.message
    assert refusal.message.endswith(CONTACT_ROUTE)


def test_the_example_matches_however_the_numbers_are_spelled():
    spelled = copy.deepcopy(ADELAIDE)
    spelled["tx"]["latitude"] = -34.9810
    spelled["rx"]["altitude"] = 50.0
    spelled["rx"]["latitude"] = -34.92861
    assert _refusal_of(parse_config, _config(**spelled)).code == Blah2Refusal.EXAMPLE_CONFIG


def test_the_example_matches_on_position_whatever_the_names():
    renamed = copy.deepcopy(ADELAIDE)
    renamed["rx"]["name"] = "Home"
    assert _refusal_of(parse_config, _config(**renamed)).code == Blah2Refusal.EXAMPLE_CONFIG


def test_an_adelaide_receiver_with_its_own_transmitter_is_accepted():
    location = {"rx": ADELAIDE["rx"], "tx": STOCK["location"]["tx"]}
    assert parse_config(_config(**location)).rx.name == "Adelaide"


# ── Config fingerprint ────────────────────────────────────────────────────────


def test_the_fingerprint_hashes_a_canonical_form():
    canonical = (
        '{"fc":204640000.0,'
        '"rx":{"altitude":12.0,"latitude":10.5,"longitude":-30.25,"name":"Receiver"},'
        '"tx":{"altitude":300.0,"latitude":10.75,"longitude":-30.5,"name":"Transmitter"}}'
    )
    assert config_fingerprint(STOCK) == hashlib.sha256(canonical.encode()).hexdigest()


def test_the_fingerprint_ignores_number_spelling():
    spelled = _set(STOCK, ("location", "rx", "altitude"), 12.0)
    spelled = _set(spelled, ("capture", "fc"), 2.0464e8)
    spelled = _set(spelled, ("location", "tx", "latitude"), 10.750)
    assert config_fingerprint(spelled) == config_fingerprint(STOCK)


def test_the_fingerprint_treats_negative_zero_as_zero():
    a = _set(STOCK, ("location", "rx", "longitude"), 0.0)
    b = _set(STOCK, ("location", "rx", "longitude"), -0.0)
    assert config_fingerprint(a) == config_fingerprint(b)


@pytest.mark.parametrize(
    "path, value",
    [
        (("capture", "fs"), 3000000),
        (("capture", "device", "type"), "HackRF"),
        (("process", "data", "cpi"), 1.5),
        (("network", "ports", "api"), 3100),
        (("truth", "adsb", "enabled"), False),
        (("save", "iq"), False),
        (("location", "rx", "note"), "extra"),
        (("extra",), {"anything": 1}),
    ],
)
def test_irrelevant_keys_leave_the_fingerprint_alone(path, value):
    assert config_fingerprint(_set(STOCK, path, value)) == config_fingerprint(STOCK)


@pytest.mark.parametrize(
    "path, value",
    [
        (("location", "rx", "latitude"), 10.5001),
        (("location", "rx", "longitude"), -30.2501),
        (("location", "rx", "altitude"), 13),
        (("location", "rx", "name"), "Elsewhere"),
        (("location", "tx", "latitude"), 10.7501),
        (("location", "tx", "longitude"), -30.5001),
        (("location", "tx", "altitude"), 301),
        (("location", "tx", "name"), "Other tower"),
        (("capture", "fc"), 204640001),
    ],
)
def test_each_stable_key_moves_the_fingerprint(path, value):
    assert config_fingerprint(_set(STOCK, path, value)) != config_fingerprint(STOCK)


def test_the_fingerprint_refuses_a_config_that_is_not_blah2():
    assert _refusal_of(config_fingerprint, {"hello": "world"}).code == Blah2Refusal.NOT_BLAH2


# ── /api/detection ────────────────────────────────────────────────────────────


def test_a_detection_frame_parses():
    frame = parse_detection(_frame(1_700_000_000_123, n=3))
    assert frame.timestamp_ms == 1_700_000_000_123
    assert frame.delay_km == (12.5,) * 3
    assert frame.doppler_hz == (-40.0,) * 3
    assert frame.snr_db == (14.2,) * 3


def test_an_empty_frame_is_valid():
    frame = parse_detection(_frame(1_700_000_000_000, n=0))
    assert (frame.delay_km, frame.doppler_hz, frame.snr_db) == ((), (), ())


def test_the_first_frame_after_an_api_restart_loses_its_undefined_prefix():
    assert parse_detection(b"undefined" + _frame(1_700_000_000_000)).timestamp_ms == 1_700_000_000_000


@pytest.mark.parametrize("body", [b"", b"  \n"])
def test_an_empty_body_means_no_detection_yet(body):
    refusal = _refusal_of(parse_detection, body)
    assert refusal.code == Blah2Refusal.NO_DETECTION_YET
    assert "not produced a detection yet" in refusal.message


@pytest.mark.parametrize(
    "body",
    [
        b"<html>hello</html>",
        b"undefined",
        b"undefinedundefined" + _frame(1_700_000_000_000),
        b"[]",
        b'{"delay": [], "doppler": [], "snr": []}',
        b'{"timestamp": "1700000000000", "delay": [], "doppler": [], "snr": []}',
        b'{"timestamp": true, "delay": [], "doppler": [], "snr": []}',
        b'{"timestamp": 1700000000000.5, "delay": [], "doppler": [], "snr": []}',
        b'{"timestamp": -1, "delay": [], "doppler": [], "snr": []}',
        b'{"timestamp": 1700000000000, "delay": [1.0], "doppler": [], "snr": []}',
        b'{"timestamp": 1700000000000, "delay": ["1"], "doppler": [2], "snr": [3]}',
        b'{"timestamp": 1700000000000, "delay": [NaN], "doppler": [2], "snr": [3]}',
        b'{"timestamp": 1700000000000, "delay": 1, "doppler": 2, "snr": 3}',
        b'{"timestamp": 1700000000000, "delay": [], "doppler": []}',
        b"[" * 100_000,
    ],
)
def test_bodies_that_are_not_a_blah2_frame_are_refused(body):
    assert _refusal_of(parse_detection, body).code == Blah2Refusal.NOT_BLAH2


# ── The probe, against a stub radar ───────────────────────────────────────────


@pytest.fixture(autouse=True)
def _short_gap(monkeypatch):
    monkeypatch.setattr(blah2_probe, "freshness_gap_s", lambda cpi_s: 0.05)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _serve_json(payload):
    async def route():
        return json.dumps(payload).encode()

    return route


def _serve_frames(offset_s: float = 0.0):
    async def route():
        return _frame(_now_ms() + int(offset_s * 1000))

    return route


async def _probe(stub: StubServer, **kwargs):
    kwargs.setdefault("resolver", resolve_to_loopback)
    kwargs.setdefault("policy", only_loopback)
    return await probe_blah2(parse_endpoint(f"radar.example.com:{stub.port}"), **kwargs)


async def _probe_refusal(stub: StubServer, **kwargs) -> EndpointRefused:
    with pytest.raises(EndpointRefused) as info:
        await _probe(stub, **kwargs)
    return info.value


async def test_a_live_stock_blah2_passes():
    async with StubServer({CONFIG_PATH: _serve_json(STOCK), DETECTION_PATH: _serve_frames()}) as stub:
        result = await _probe(stub)
    assert result.endpoint.endpoint_key == f"radar.example.com:{stub.port}"
    assert result.rx == Site(10.5, -30.25, 12.0, "Receiver")
    assert result.tx == Site(10.75, -30.5, 300.0, "Transmitter")
    assert (result.fc_hz, result.fs_hz, result.cpi_s) == (204640000.0, 2000000.0, 0.75)
    assert result.config_fingerprint == config_fingerprint(STOCK)
    assert result.address == "127.0.0.1"
    assert abs(result.clock_offset_s) < 2


@pytest.mark.parametrize(
    ("cpi_s", "gap_s"),
    [(None, 1.75), (0.5, 1.75), (1.5, 3.0), (2.5, 5.0), (60.0, 5.0)],
)
def test_the_gap_between_reads_spans_two_cpis_within_bounds(cpi_s, gap_s):
    assert freshness_gap_s(cpi_s) == gap_s


@pytest.mark.parametrize("cpi", [0, -1, "0.75", None])
def test_a_cpi_that_is_not_a_positive_number_is_left_out(cpi):
    assert parse_config(_set(STOCK, ("process", "data", "cpi"), cpi)).cpi_s is None


async def test_the_probe_waits_by_the_radars_own_cpi(monkeypatch):
    seen = []

    def gap(cpi_s):
        seen.append(cpi_s)
        return 0.05

    monkeypatch.setattr(blah2_probe, "freshness_gap_s", gap)
    slow = _set(STOCK, ("process", "data", "cpi"), 2.0)
    async with StubServer({CONFIG_PATH: _serve_json(slow), DETECTION_PATH: _serve_frames()}) as stub:
        await _probe(stub)
    assert seen == [2.0]


async def test_the_probe_requests_only_config_and_detection():
    async with StubServer({CONFIG_PATH: _serve_json(STOCK), DETECTION_PATH: _serve_frames()}) as stub:
        await _probe(stub)
    assert stub.paths == [CONFIG_PATH, DETECTION_PATH, DETECTION_PATH]
    assert {r.method for r in stub.requests} == {"GET"}
    assert {r.headers["host"] for r in stub.requests} == {f"radar.example.com:{stub.port}"}


async def test_the_undefined_prefix_quirk_does_not_fail_the_probe():
    served = []

    async def detection():
        body = _frame(_now_ms())
        served.append(body)
        return b"undefined" + body if len(served) == 1 else body

    async with StubServer({CONFIG_PATH: _serve_json(STOCK), DETECTION_PATH: detection}) as stub:
        result = await _probe(stub)
    assert result.address == "127.0.0.1"


async def test_blah2_arm_is_refused_after_config_alone():
    config = {"truth": {"adsb": {"enabled": True}}}
    async with StubServer({CONFIG_PATH: _serve_json(config), DETECTION_PATH: _serve_frames()}) as stub:
        refusal = await _probe_refusal(stub)
    assert refusal.code == Blah2Refusal.BLAH2_ARM
    assert stub.paths == [CONFIG_PATH]


async def test_the_example_config_is_refused_after_config_alone():
    async with StubServer({CONFIG_PATH: _serve_json(_config(**ADELAIDE)), DETECTION_PATH: _serve_frames()}) as stub:
        refusal = await _probe_refusal(stub)
    assert refusal.code == Blah2Refusal.EXAMPLE_CONFIG
    assert stub.paths == [CONFIG_PATH]


async def test_a_config_that_is_not_json_is_not_blah2():
    async def html():
        return b"<html><body>router login</body></html>"

    async with StubServer({CONFIG_PATH: html}) as stub:
        assert (await _probe_refusal(stub)).code == Blah2Refusal.NOT_BLAH2


async def test_a_missing_config_is_a_bad_response():
    async with StubServer({}) as stub:
        assert (await _probe_refusal(stub)).code == Refusal.BAD_RESPONSE


async def test_a_radar_that_has_not_detected_yet_is_refused():
    async def empty():
        return b""

    async with StubServer({CONFIG_PATH: _serve_json(STOCK), DETECTION_PATH: empty}) as stub:
        refusal = await _probe_refusal(stub)
    assert refusal.code == Blah2Refusal.NO_DETECTION_YET


async def test_a_frame_that_does_not_advance_is_stalled():
    stale = _now_ms() - 3_600_000

    async def frozen():
        return _frame(stale)

    async with StubServer({CONFIG_PATH: _serve_json(STOCK), DETECTION_PATH: frozen}) as stub:
        refusal = await _probe_refusal(stub)
    assert refusal.code == Blah2Refusal.STALLED


@pytest.mark.parametrize("offset_s, word", [(-60, "behind"), (60, "ahead")])
async def test_a_skewed_radar_clock_is_refused_with_the_offset(offset_s, word):
    async with StubServer({CONFIG_PATH: _serve_json(STOCK), DETECTION_PATH: _serve_frames(offset_s)}) as stub:
        refusal = await _probe_refusal(stub)
    assert refusal.code == Blah2Refusal.CLOCK_OFFSET
    assert "60 s" in refusal.message
    assert word in refusal.message
    assert "NTP" in refusal.message


async def test_a_small_clock_offset_is_accepted():
    async with StubServer({CONFIG_PATH: _serve_json(STOCK), DETECTION_PATH: _serve_frames(-5)}) as stub:
        result = await _probe(stub)
    assert -6 < result.clock_offset_s < -4


async def test_an_oversized_detection_is_refused():
    async def huge():
        return b" " * (blah2_probe.DETECTION_MAX_BYTES + 1)

    async with StubServer({CONFIG_PATH: _serve_json(STOCK), DETECTION_PATH: huge}) as stub:
        assert (await _probe_refusal(stub)).code == Refusal.TOO_LARGE


async def test_the_whole_probe_is_bounded(monkeypatch):
    monkeypatch.setattr(blah2_probe, "PROBE_DEADLINE_S", 0.3)
    monkeypatch.setattr(polled_endpoint, "REQUEST_DEADLINE_S", 30)

    async def hang():
        await asyncio.sleep(30)
        return b""

    async with StubServer({CONFIG_PATH: _serve_json(STOCK), DETECTION_PATH: hang}) as stub:
        started = time.monotonic()
        refusal = await _probe_refusal(stub)
    assert refusal.code == Refusal.TIMED_OUT
    assert time.monotonic() - started < 5


async def test_the_default_policy_keeps_the_probe_off_loopback():
    async with StubServer({CONFIG_PATH: _serve_json(STOCK), DETECTION_PATH: _serve_frames()}) as stub:
        with pytest.raises(EndpointRefused) as info:
            await probe_blah2(parse_endpoint(f"127.0.0.1:{stub.port}"))
    assert info.value.code == Refusal.ADDRESS_NOT_PUBLIC
    assert stub.requests == []
