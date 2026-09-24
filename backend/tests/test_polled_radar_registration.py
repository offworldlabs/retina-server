"""Tests for an owner registering a stock blah2 radar from the app.

Registration is the switch for every polled radar, so it answers only where
POLLED_RADAR_REGISTRATION_ENABLED is exactly `1`. The owner's submit probes the
radar again and writes the rows only if it still declares what the owner
confirmed, and every probe is bounded per account and across the server, since
each one is a request our server sends to a host of the caller's choosing.
"""

import asyncio
import base64
import copy
import json
import re
import time
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from core import state
from core.node_ids import node_id_pattern
from core.nodes import Node, NodeClaim, NodeConfig, PolledRadar
from core.users import ANONYMOUS_USER, async_session_maker
from services import (
    blah2_poller,
    blah2_probe,
    node_retirement,
    polled_radars,
    polled_registration,
    probation,
    publication,
)
from services.blah2_probe import CONFIG_PATH, DETECTION_PATH, Blah2Probe, Site, config_fingerprint
from services.node_rate_limits import polled_probe_limiter
from services.polled_radars import EndpointAlreadyRegistered, create_polled_radar
from tests.ownership_helpers import own
from tests.radar_stub import STOCK_CONFIG, StubServer, only_loopback, resolve_to_loopback
from tests.test_polled_radars import _args as _row_args

OWNER = {"id": "user-a", "email": "ada@example.com"}
STOCK_FINGERPRINT = config_fingerprint(STOCK_CONFIG)

ADELAIDE = {
    "rx": {"latitude": -34.9286, "longitude": 138.5999, "altitude": 50, "name": "Adelaide"},
    "tx": {"latitude": -34.981, "longitude": 138.7081, "altitude": 750, "name": "Mount Lofty"},
}


@pytest.fixture(autouse=True)
def _radars_on_loopback(monkeypatch):
    monkeypatch.setattr(polled_registration, "resolver", resolve_to_loopback)
    monkeypatch.setattr(polled_registration, "policy", only_loopback)
    monkeypatch.setattr(blah2_probe, "freshness_gap_s", lambda cpi_s: 0.05)
    polled_probe_limiter.reset()
    yield
    polled_probe_limiter.reset()


@pytest.fixture()
def registration_open(monkeypatch):
    monkeypatch.setenv(polled_registration.ENABLED_ENV, "1")


@pytest.fixture()
def secret_key(monkeypatch):
    monkeypatch.setenv("POLLED_RADAR_SECRET_KEY", Fernet.generate_key().decode())


def _basic(user: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


def _radar(config: dict = STOCK_CONFIG, *, demands: str | None = None) -> StubServer:
    """A live stock blah2, answering only requests carrying `demands` as their
    Authorization header when one is given."""
    stub = StubServer({})

    def guarded(body):
        async def route():
            if demands is not None and stub.requests[-1].headers.get("authorization") != demands:
                return (401, b"")
            return body()

        return route

    stub.routes = {
        CONFIG_PATH: guarded(lambda: json.dumps(config).encode()),
        DETECTION_PATH: guarded(
            lambda: json.dumps(
                {"timestamp": int(time.time() * 1000), "delay": [12.5], "doppler": [-40.0], "snr": [14.2]}
            ).encode()
        ),
    }
    return stub


def _declaring(**changes) -> dict:
    """STOCK_CONFIG with `changes` applied, each keyed by a dotted path; None deletes."""
    config = copy.deepcopy(STOCK_CONFIG)
    for path, value in changes.items():
        *parents, leaf = path.split(".")
        node = config
        for key in parents:
            node = node[key]
        if value is None:
            del node[leaf]
        else:
            node[leaf] = value
    return config


async def _refused(session, address: str, user_id: str = "user-a") -> polled_registration.RegistrationRefused:
    with pytest.raises(polled_registration.RegistrationRefused) as info:
        await polled_registration.probe(session, address, user_id)
    return info.value


def _quota_left(user_id: str = "user-a") -> int:
    left = 0
    while polled_probe_limiter.admit(user_id) is None:
        left += 1
    return left


async def _radars(session) -> int:
    return (await session.execute(select(func.count()).select_from(PolledRadar))).scalar_one()


@pytest.mark.parametrize(("value", "on"), [("1", True), ("0", False), ("true", False), ("", False)])
def test_registration_opens_only_on_exactly_one(monkeypatch, value, on):
    monkeypatch.setenv(polled_registration.ENABLED_ENV, value)
    assert polled_registration.enabled() is on


def test_the_switch_is_off_when_unset(monkeypatch):
    monkeypatch.delenv(polled_registration.ENABLED_ENV, raising=False)
    assert polled_registration.enabled() is False


def test_health_and_the_session_report_the_switch(client, monkeypatch):
    monkeypatch.setenv(polled_registration.ENABLED_ENV, "1")
    assert client.get("/api/health").json()["polled_radar_registration"] is True
    assert client.get("/api/auth/me").json()["polled_radar_registration"] is True
    monkeypatch.delenv(polled_registration.ENABLED_ENV)
    assert client.get("/api/health").json()["polled_radar_registration"] is False
    assert client.get("/api/auth/me").json()["polled_radar_registration"] is False


# ── Probing ──────────────────────────────────────────────────────────────────


async def test_a_live_radar_is_reported_as_it_declares_itself(node_session):
    async with _radar() as stub:
        probed = await polled_registration.probe(node_session, f"radar.example.com:{stub.port}", "user-a")

    assert probed.public() == {
        "address": f"http://radar.example.com:{stub.port}",
        "rx": {"latitude": 10.5, "longitude": -30.25, "altitude_m": 12.0, "name": "Receiver"},
        "tx": {"latitude": 10.75, "longitude": -30.5, "altitude_m": 300.0, "name": "Transmitter"},
        "fc_hz": 204640000.0,
        "fs_hz": 2000000.0,
        "cpi_s": 0.75,
        "fingerprint": config_fingerprint(STOCK_CONFIG),
        "protected": False,
    }
    assert await _radars(node_session) == 0
    assert polled_registration._in_flight == 0


async def test_what_blah2_leaves_out_takes_the_fleets_defaults(node_session):
    async with _radar(_declaring(**{"capture.fs": None, "process.data.cpi": None})) as stub:
        probed = await polled_registration.probe(node_session, f"radar.example.com:{stub.port}", "user-a")

    assert (probed.public()["fs_hz"], probed.public()["cpi_s"]) == (2_000_000.0, 0.5)
    assert {k: probed.config[k] for k in polled_registration.REGISTRATION_BASE} == {
        "fs_hz": 2_000_000.0,
        "cpi_s": 0.5,
        "delay_tolerance_us": 2.0,
        "doppler_tolerance_hz": 5.0,
        "max_range_km": 50.0,
        "beam_width_deg": None,
        "beam_azimuth_deg": None,
        "tx_callsign": None,
    }


async def test_a_password_the_radar_ignores_protects_nothing(node_session, secret_key):
    async with _radar() as stub:
        probed = await polled_registration.probe(
            node_session, f"http://owner:hunter2@radar.example.com:{stub.port}", "user-a"
        )

    assert probed.protected is False
    assert stub.requests[-1].headers.get("authorization") is None


async def test_a_password_the_radar_demands_protects_it_and_is_never_shown(node_session, secret_key):
    async with _radar(demands=_basic("owner", "hunter2")) as stub:
        probed = await polled_registration.probe(
            node_session, f"http://owner:hunter2@radar.example.com:{stub.port}", "user-a"
        )

    assert probed.protected is True
    assert "hunter2" not in json.dumps(probed.public())
    assert probed.public()["address"] == f"http://radar.example.com:{stub.port}"


async def test_a_password_with_nowhere_to_keep_it_is_refused_before_any_request(node_session, monkeypatch):
    monkeypatch.delenv("POLLED_RADAR_SECRET_KEY", raising=False)
    async with _radar() as stub:
        refusal = await _refused(node_session, f"http://owner:hunter2@radar.example.com:{stub.port}")

    assert (refusal.status, refusal.code) == (503, "credentials_unavailable")
    assert stub.requests == []
    assert _quota_left() == 10


async def test_an_address_already_registered_is_refused_before_any_request_for_a_probe(node_session):
    """Before any request, but metered: the answer tells the caller whether an
    address is one of our radars, so asking it as often as they like would
    map the registry."""
    async with _radar() as stub:
        key = f"radar.example.com:{stub.port}"
        await create_polled_radar(node_session, **_row_args(endpoint_key=key, port=stub.port))
        await node_session.commit()
        refusal = await _refused(node_session, key)
        for _ in range(9):
            polled_probe_limiter.admit("user-a")
        metered = await _refused(node_session, key)

    assert (refusal.status, refusal.code) == (409, "endpoint_registered")
    assert stub.requests == []
    assert (metered.status, metered.code) == (429, "rate_limited")
    assert polled_registration._in_flight == 0


async def test_an_address_that_is_not_one_is_refused_without_spending_a_probe(node_session):
    refusal = await _refused(node_session, "radar.example.com:3000/api")

    assert (refusal.status, refusal.code) == (422, "path_not_supported")
    assert _quota_left() == 10


async def test_the_eleventh_probe_in_an_hour_is_refused_and_another_account_is_not(node_session):
    for _ in range(10):
        polled_probe_limiter.admit("user-a")
    async with _radar() as stub:
        refusal = await _refused(node_session, f"radar.example.com:{stub.port}")
        assert stub.requests == []
        await polled_registration.probe(node_session, f"radar.example.com:{stub.port}", "user-b")

    assert (refusal.status, refusal.code) == (429, "rate_limited")
    assert refusal.retry_after_s >= 1


async def test_a_probe_beyond_those_in_flight_is_turned_away_with_its_quota_unspent(node_session, monkeypatch):
    monkeypatch.setattr(polled_registration, "_in_flight", polled_registration.MAX_PROBES_IN_FLIGHT)
    async with _radar() as stub:
        refusal = await _refused(node_session, f"radar.example.com:{stub.port}")

    assert (refusal.status, refusal.code, refusal.retry_after_s) == (429, "busy", 5)
    assert stub.requests == []
    assert _quota_left() == 10


async def test_blah2s_example_configuration_is_refused_in_the_probes_words(node_session):
    async with _radar(_declaring(location=ADELAIDE)) as stub:
        refusal = await _refused(node_session, f"radar.example.com:{stub.port}")

    assert (refusal.status, refusal.code) == (422, "example_config")
    assert "hello@offworldlabs.com" in refusal.message
    assert polled_registration._in_flight == 0


async def test_a_radar_declaring_what_the_pipeline_refuses_is_refused(node_session):
    async with _radar(_declaring(**{"capture.fs": 50_000_000})) as stub:
        refusal = await _refused(node_session, f"radar.example.com:{stub.port}")

    assert (refusal.status, refusal.code) == (422, "config_invalid")
    assert "fs_hz" in refusal.message


# ── Registering ──────────────────────────────────────────────────────────────


async def _register(session, address: str, *, fingerprint: str = STOCK_FINGERPRINT, publication: str = "public"):
    return await polled_registration.register(
        session, raw=address, fingerprint=fingerprint, publication=publication, user=OWNER
    )


async def _register_refused(session, address: str, **kwargs) -> polled_registration.RegistrationRefused:
    with pytest.raises(polled_registration.RegistrationRefused) as info:
        await _register(session, address, **kwargs)
    return info.value


async def test_a_registration_writes_the_radar_as_it_declared_itself(node_session):
    async with _radar() as stub:
        registration = await _register(node_session, f"radar.example.com:{stub.port}", publication="private")
        await node_session.commit()
    node_id = registration.node.node_id
    node_session.expire_all()

    assert re.match(node_id_pattern("bla"), node_id)
    node = await node_session.get(Node, node_id)
    assert (node.publication, node.licence_version, node.licence_accepted_at) == ("private", None, None)
    config = (await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == node_id))).scalar_one()
    assert (config.rx_lat, config.rx_lon, config.tx_lat, config.tx_lon) == (10.5, -30.25, 10.75, -30.5)
    assert (config.fc_hz, config.fs_hz, config.cpi_s) == (204640000.0, 2000000.0, 0.75)
    assert (config.max_range_km, config.delay_tolerance_us, config.doppler_tolerance_hz) == (50.0, 2.0, 5.0)
    claim = await node_session.get(NodeClaim, node_id)
    assert (claim.user_id, claim.email, claim.verified) == ("user-a", "ada@example.com", True)
    radar = await node_session.get(PolledRadar, node_id)
    assert (radar.epoch, radar.trust_state, radar.unprotected) == (1, "probation", True)
    assert (radar.config_fingerprint, radar.last_resolved_ip) == (STOCK_FINGERPRINT, "127.0.0.1")


async def test_the_poller_reads_back_the_configuration_registration_wrote(node_session):
    """The poller lays each config poll over the stored version; registration must
    have stored what that reads back as unchanged, or the radar's first config
    poll writes a second version for nothing."""
    async with _radar() as stub:
        registration = await _register(node_session, f"radar.example.com:{stub.port}")
        await node_session.commit()
    stored = (
        await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == registration.node.node_id))
    ).scalar_one()
    base = {field: getattr(stored, field) for field in polled_registration.REGISTRATION_BASE}
    base |= {f: getattr(stored, f) for f in ("rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft", "fc_hz")}

    assert polled_radars.probed_config(blah2_probe.parse_config(STOCK_CONFIG), base) == base


async def test_a_protected_radar_is_registered_with_its_password_kept_for_the_poller(node_session, secret_key):
    async with _radar(demands=_basic("owner", "hunter2")) as stub:
        registration = await _register(node_session, f"http://owner:hunter2@radar.example.com:{stub.port}")

    assert registration.radar.unprotected is False
    assert polled_radars.poller_credentials(registration.radar) == ("owner", "hunter2")


async def test_a_radar_changed_since_the_owner_looked_is_shown_again_not_registered(node_session):
    moved = _declaring(**{"location.rx.latitude": 10.625})
    async with _radar(moved) as stub:
        refusal = await _register_refused(node_session, f"radar.example.com:{stub.port}")

    assert (refusal.status, refusal.code) == (409, "config_changed")
    assert refusal.probe.public()["rx"]["latitude"] == 10.625
    assert refusal.probe.public()["fingerprint"] == config_fingerprint(moved)
    assert await _radars(node_session) == 0


async def test_an_address_taken_while_the_owner_confirmed_is_refused(node_session, monkeypatch):
    async def taken_meanwhile(*args, **kwargs):
        raise EndpointAlreadyRegistered("this endpoint is already registered")

    monkeypatch.setattr(polled_registration, "create_polled_radar", taken_meanwhile)
    async with _radar() as stub:
        refusal = await _register_refused(node_session, f"radar.example.com:{stub.port}")

    assert (refusal.status, refusal.code) == (409, "endpoint_registered")


async def test_the_eleventh_radar_on_an_account_is_refused_before_any_request(node_session):
    for i in range(polled_registration.MAX_RADARS_PER_ACCOUNT):
        await create_polled_radar(
            node_session, **_row_args(endpoint_key=f"r{i}.example.com:3000", host=f"r{i}.example.com")
        )
    await node_session.commit()
    async with _radar() as stub:
        refusal = await _register_refused(node_session, f"radar.example.com:{stub.port}")

    assert (refusal.status, refusal.code) == (403, "radar_limit")
    assert stub.requests == []


# ── A new address ────────────────────────────────────────────────────────────


async def _registered(session, stub: StubServer) -> str:
    registration = await _register(session, f"radar.example.com:{stub.port}")
    await session.commit()
    return registration.node.node_id


async def _move(session, node_id: str, address: str, *, fingerprint: str = STOCK_FINGERPRINT, user: dict = OWNER):
    return await polled_registration.change_address(session, node_id, raw=address, fingerprint=fingerprint, user=user)


async def _move_refused(session, node_id: str, address: str, **kwargs) -> polled_registration.RegistrationRefused:
    with pytest.raises(polled_registration.RegistrationRefused) as info:
        await _move(session, node_id, address, **kwargs)
    return info.value


async def test_an_owner_moves_their_radar_after_seeing_what_it_declares_there(node_session):
    async with _radar() as old, _radar() as new:
        node_id = await _registered(node_session, old)
        address = f"other.example.com:{new.port}"
        probed = await polled_registration.probe_new_address(node_session, node_id, address, "user-a")
        radar = await _move(node_session, node_id, address, fingerprint=probed.public()["fingerprint"])
        await node_session.commit()

    assert (radar.node_id, radar.epoch, radar.trust_state) == (node_id, 2, "probation")
    assert radar.endpoint_key == f"other.example.com:{new.port}"


async def test_a_radars_own_address_is_not_taken_from_it(node_session, secret_key):
    async with _radar() as stub:
        node_id = await _registered(node_session, stub)
        address = f"http://owner:hunter2@radar.example.com:{stub.port}"
        await polled_registration.probe_new_address(node_session, node_id, address, "user-a")
        radar = await _move(node_session, node_id, address)
        await node_session.commit()

    assert radar.epoch == 1
    assert polled_radars.poller_credentials(radar) == ("owner", "hunter2")


async def test_a_radar_that_is_not_the_callers_is_not_found_and_costs_them_nothing(node_session):
    async with _radar() as stub:
        node_id = await _registered(node_session, stub)
        sent = len(stub.requests)
        stranger = {"id": "user-b", "email": "bob@example.com"}
        for target in (node_id, "bla00000000"):
            with pytest.raises(polled_registration.RegistrationRefused) as info:
                await polled_registration.probe_new_address(
                    node_session, target, f"radar.example.com:{stub.port}", "user-b"
                )
            assert (info.value.status, info.value.code) == (404, "not_found")
            refusal = await _move_refused(node_session, target, f"radar.example.com:{stub.port}", user=stranger)
            assert (refusal.status, refusal.code) == (404, "not_found")

        assert len(stub.requests) == sent
    assert _quota_left("user-b") == 10


async def test_a_radar_changed_since_the_owner_looked_is_shown_again_not_moved(node_session):
    async with _radar() as old, _radar(_declaring(**{"location.rx.latitude": 10.625})) as new:
        node_id = await _registered(node_session, old)
        refusal = await _move_refused(node_session, node_id, f"other.example.com:{new.port}")
    node_session.expire_all()

    assert (refusal.status, refusal.code) == (409, "config_changed")
    assert refusal.probe.public()["rx"]["latitude"] == 10.625
    radar = await node_session.get(PolledRadar, node_id)
    assert (radar.epoch, radar.endpoint_key) == (1, f"radar.example.com:{old.port}")


async def test_an_address_another_radar_holds_is_refused_before_any_request(node_session):
    async with _radar() as mine, _radar() as theirs:
        node_id = await _registered(node_session, mine)
        await create_polled_radar(
            node_session,
            **_row_args(
                owner_user_id="user-b",
                owner_email="bob@example.com",
                endpoint_key=f"other.example.com:{theirs.port}",
                host="other.example.com",
                port=theirs.port,
            ),
        )
        await node_session.commit()
        refusal = await _move_refused(node_session, node_id, f"other.example.com:{theirs.port}")

    assert (refusal.status, refusal.code) == (409, "endpoint_registered")
    assert theirs.requests == []


async def test_the_poller_moving_on_meanwhile_sends_the_owner_back_to_look_again(node_session, monkeypatch):
    async def moved_on(*args, **kwargs):
        raise polled_radars.EpochChanged("moved on")

    monkeypatch.setattr(polled_registration, "set_polled_radar_address", moved_on)
    async with _radar() as old, _radar() as new:
        node_id = await _registered(node_session, old)
        refusal = await _move_refused(node_session, node_id, f"other.example.com:{new.port}")

    assert (refusal.status, refusal.code) == (409, "config_changed")
    assert refusal.probe.public()["fingerprint"] == STOCK_FINGERPRINT


async def test_moving_a_radar_draws_on_the_same_allowance_as_registering_one(node_session):
    async with _radar() as stub:
        node_id = await _registered(node_session, stub)
        while polled_probe_limiter.admit("user-a") is None:
            pass
        with pytest.raises(polled_registration.RegistrationRefused) as info:
            await polled_registration.probe_new_address(node_session, node_id, "other.example.com:4000", "user-a")

    assert (info.value.status, info.value.code) == (429, "rate_limited")


# ── Routes ───────────────────────────────────────────────────────────────────

PROBE = "/api/auth/me/polled-radars/probe"
REGISTER = "/api/auth/me/polled-radars"
ADDRESS = "radar.example.com:3000"
SUBMIT = {"address": ADDRESS, "fingerprint": STOCK_FINGERPRINT, "publication": "public"}


def _canned(fingerprint: str = STOCK_FINGERPRINT):
    """probe_blah2 answering as a live stock radar would, with no network."""

    async def probe_blah2(endpoint, *, resolver, policy):
        return Blah2Probe(
            endpoint=endpoint,
            rx=Site(10.5, -30.25, 12.0, "Receiver"),
            tx=Site(10.75, -30.5, 300.0, "Transmitter"),
            fc_hz=204640000.0,
            fs_hz=2000000.0,
            cpi_s=0.75,
            config_fingerprint=fingerprint,
            address="192.0.2.10",
            clock_offset_s=0.0,
        )

    return probe_blah2


@pytest.fixture()
def canned_radar(monkeypatch):
    monkeypatch.setattr(polled_registration, "probe_blah2", _canned())


@pytest.fixture()
def poller_wakes(monkeypatch):
    wakes = []
    monkeypatch.setattr(blah2_poller, "refresh", lambda: wakes.append(True))
    return wakes


@pytest.mark.parametrize(
    ("path", "body"), [(PROBE, {"address": ADDRESS}), (REGISTER, SUBMIT), (PROBE, {}), (REGISTER, {})]
)
def test_both_routes_are_absent_while_registration_is_closed(client, monkeypatch, canned_radar, path, body):
    monkeypatch.delenv(polled_registration.ENABLED_ENV, raising=False)
    assert client.post(path, json=body).status_code == 404


@pytest.mark.parametrize(("path", "body"), [(PROBE, {"address": ADDRESS}), (REGISTER, SUBMIT)])
def test_both_routes_need_a_session(client, registration_open, canned_radar, path, body):
    with patch("core.users.AUTH_BYPASS", False):
        assert client.post(path, json=body).status_code == 401


def test_a_probe_answers_what_the_radar_declares(client, registration_open, canned_radar):
    r = client.post(PROBE, json={"address": ADDRESS})

    assert r.status_code == 200
    assert r.json()["address"] == "http://radar.example.com:3000"
    assert r.json()["fingerprint"] == STOCK_FINGERPRINT
    assert r.json()["rx"] == {"latitude": 10.5, "longitude": -30.25, "altitude_m": 12.0, "name": "Receiver"}


def test_a_refusal_carries_its_code_and_the_owners_message(client, registration_open, canned_radar):
    r = client.post(PROBE, json={"address": "radar.example.com:3000/api"})

    assert r.status_code == 422
    assert r.json()["code"] == "path_not_supported"
    assert "path" in r.json()["detail"]


def test_a_rate_limited_probe_says_when_to_try_again(client, registration_open, canned_radar):
    for _ in range(10):
        polled_probe_limiter.admit(ANONYMOUS_USER["id"])

    r = client.post(PROBE, json={"address": ADDRESS})

    assert (r.status_code, r.json()["code"]) == (429, "rate_limited")
    assert int(r.headers["retry-after"]) >= 1


def test_a_registered_radar_is_listed_at_once_and_the_poller_woken(
    client, registration_open, canned_radar, poller_wakes, monkeypatch
):
    # With the fence off only the owner's choice hides the radar, read through
    # the public flush's cache; read once first, so it holds the registry as it was.
    monkeypatch.setenv("POLLED_RADAR_PROBATION_ENABLED", "0")
    publication.is_private("bla00000000")

    r = client.post(REGISTER, json=SUBMIT | {"publication": "private"})

    assert r.status_code == 201
    node_id = r.json()["node_id"]
    assert r.json() == {"node_id": node_id, "epoch": 1, "trust_state": "probation"}
    assert poller_wakes == [True]
    mine = {n["node_id"]: n for n in client.get("/api/auth/me/nodes").json()}
    assert mine[node_id]["location_private"] is True
    assert publication.is_private(node_id)


def test_a_changed_radar_answers_with_what_it_declares_now(client, registration_open, monkeypatch, poller_wakes):
    monkeypatch.setattr(polled_registration, "probe_blah2", _canned(fingerprint="b" * 64))

    r = client.post(REGISTER, json=SUBMIT)

    assert (r.status_code, r.json()["code"]) == (409, "config_changed")
    assert r.json()["probe"]["fingerprint"] == "b" * 64
    assert poller_wakes == []


def test_a_publication_other_than_public_or_private_is_refused(client, registration_open, canned_radar):
    assert client.post(REGISTER, json=SUBMIT | {"publication": "fuzzed"}).status_code == 422


OTHER = "other.example.com:3000"


def _registered_by_route(client) -> str:
    return client.post(REGISTER, json=SUBMIT).json()["node_id"]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", f"{REGISTER}/bla00000000/probe", {"address": OTHER}),
        ("put", f"{REGISTER}/bla00000000/address", {"address": OTHER, "fingerprint": STOCK_FINGERPRINT}),
    ],
)
def test_moving_a_radar_needs_a_session(client, registration_open, canned_radar, method, path, body):
    with patch("core.users.AUTH_BYPASS", False):
        assert client.request(method, path, json=body).status_code == 401


def test_a_radar_cannot_be_moved_while_registration_is_closed(
    client, registration_open, canned_radar, poller_wakes, monkeypatch
):
    node_id = _registered_by_route(client)
    monkeypatch.delenv(polled_registration.ENABLED_ENV)

    for r in (
        client.post(f"{REGISTER}/{node_id}/probe", json={"address": OTHER}),
        client.put(f"{REGISTER}/{node_id}/address", json={"address": OTHER, "fingerprint": STOCK_FINGERPRINT}),
    ):
        # The switch's answer, not the one for a radar that is not the caller's.
        assert (r.status_code, r.json()) == (404, {"detail": "Not Found"})


def test_a_probe_of_a_new_address_answers_what_the_radar_declares_there(
    client, registration_open, canned_radar, poller_wakes
):
    node_id = _registered_by_route(client)

    r = client.post(f"{REGISTER}/{node_id}/probe", json={"address": OTHER})

    assert r.status_code == 200
    assert (r.json()["address"], r.json()["fingerprint"]) == (f"http://{OTHER}", STOCK_FINGERPRINT)


def test_a_moved_radar_is_back_on_probation_at_once_and_the_poller_woken(
    client, registration_open, canned_radar, poller_wakes
):
    node_id = _registered_by_route(client)
    graduated = client.put(f"/api/admin/polled-radars/{node_id}/trust", json={"trust_state": "graduated", "epoch": 1})
    assert graduated.status_code == 200
    # Read once, so the fence's cache holds the radar as graduated.
    assert probation.in_probation(node_id) is False

    r = client.put(f"{REGISTER}/{node_id}/address", json={"address": OTHER, "fingerprint": STOCK_FINGERPRINT})

    assert r.status_code == 200
    assert r.json() == {"node_id": node_id, "epoch": 2, "trust_state": "probation"}
    assert probation.in_probation(node_id) is True
    assert poller_wakes == [True, True]


def test_a_radar_that_is_not_the_callers_answers_as_one_that_does_not_exist(client, registration_open, canned_radar):
    for r in (
        client.post(f"{REGISTER}/bla00000000/probe", json={"address": OTHER}),
        client.put(f"{REGISTER}/bla00000000/address", json={"address": OTHER, "fingerprint": STOCK_FINGERPRINT}),
    ):
        assert (r.status_code, r.json()["code"]) == (404, "not_found")


# ── Removing a radar ─────────────────────────────────────────────────────────


def test_releasing_a_polled_radar_removes_it_whatever_the_switch(
    client, registration_open, canned_radar, poller_wakes, monkeypatch
):
    node_id = _registered_by_route(client)
    with state.connected_nodes_lock:
        state.connected_nodes[node_id] = {"status": "active", "config": {}}
    monkeypatch.delenv(polled_registration.ENABLED_ENV)

    r = client.delete(f"/api/auth/me/nodes/{node_id}/claim")

    assert r.status_code == 200
    assert node_id not in {n["node_id"] for n in client.get("/api/auth/me/nodes").json()}
    assert node_id not in state.connected_nodes
    assert poller_wakes == [True, True]

    async def status():
        async with async_session_maker() as session:
            return (await session.get(Node, node_id)).status

    assert asyncio.run(status()) == "retired"


def test_releasing_a_fleet_node_leaves_it_connected_for_its_next_owner(client, poller_wakes):
    own("ret1a2b3c4d", ANONYMOUS_USER["id"])
    with state.connected_nodes_lock:
        state.connected_nodes["ret1a2b3c4d"] = {"status": "active", "config": {}}

    assert client.delete("/api/auth/me/nodes/ret1a2b3c4d/claim").status_code == 200
    assert "ret1a2b3c4d" in state.connected_nodes
    assert poller_wakes == []


def test_a_removal_the_memory_cannot_all_be_cleared_from_still_answers_ok(
    client, registration_open, canned_radar, poller_wakes, monkeypatch
):
    node_id = _registered_by_route(client)

    def half_retired(node_id):
        raise OSError("coverage file")

    monkeypatch.setattr(node_retirement, "forget_node", half_retired)

    assert client.delete(f"/api/auth/me/nodes/{node_id}/claim").status_code == 200
    assert poller_wakes == [True, True]


def test_an_administrator_cannot_leave_a_polled_radar_without_an_owner(
    client, registration_open, canned_radar, poller_wakes
):
    node_id = _registered_by_route(client)

    r = client.put(f"/api/admin/nodes/{node_id}/owner", json={"user_id": None})

    assert r.status_code == 409
    assert node_id in {n["node_id"] for n in client.get("/api/auth/me/nodes").json()}
