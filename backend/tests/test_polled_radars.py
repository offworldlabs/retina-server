import copy
import re
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select, text

from core.node_ids import node_id_pattern
from core.nodes import Node, NodeClaim, NodeConfig, PolledRadar, PolledRadarEndpointHistory
from core.secrets import SecretKeyUnavailable
from services.blah2_probe import parse_config
from services.node_config import ConfigInvalid
from services.polled_radars import (
    EndpointAlreadyRegistered,
    RadarGeometry,
    create_polled_radar,
    poller_credentials,
    probed_config,
)
from tests.radar_stub import STOCK_CONFIG

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
# SQLite returns a timezone-aware column naive, as UTC wall time.
STORED_NOW = NOW.replace(tzinfo=None)
FINGERPRINT = "a" * 64


def _args(**overrides):
    args = {
        "owner_user_id": "user-a",
        "owner_email": "ada@example.com",
        "endpoint_raw": "http://radar.example.com:3000/",
        "scheme": "http",
        "host": "radar.example.com",
        "port": 3000,
        "endpoint_key": "radar.example.com:3000",
        "auth_user": None,
        "auth_secret": None,
        "geometry": RadarGeometry(
            rx_lat=51.42, rx_lon=-0.91, rx_alt_m=30.48, tx_lat=51.37, tx_lon=-0.88, tx_alt_m=None
        ),
        "fc_hz": 5.7e8,
        "fs_hz": 2.0e6,
        "max_range_km": 150.0,
        "cpi_s": 0.5,
        "delay_tolerance_us": 2.0,
        "doppler_tolerance_hz": 5.0,
        "config_fingerprint": FINGERPRINT,
        "resolved_ip": "192.0.2.10",
        "publication": "public",
        "licence_version": "eula-1",
        "unprotected": True,
        "now": NOW,
    }
    return args | overrides


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.fixture
def secret_key(monkeypatch):
    monkeypatch.setenv("POLLED_RADAR_SECRET_KEY", Fernet.generate_key().decode())


async def test_registration_writes_all_five_rows(node_session):
    registration = await create_polled_radar(node_session, **_args())
    await node_session.commit()
    node_id = registration.node.node_id
    node_session.expire_all()

    assert re.match(node_id_pattern("bla"), node_id)

    node = await node_session.get(Node, node_id)
    assert node.board_model == "blah2"
    assert node.status == "active"
    assert node.node_ref.startswith("nde")
    assert node.active_config_version == 1
    assert node.publication == "public"
    assert node.licence_version == "eula-1"
    assert node.licence_accepted_at is not None
    assert node.remote_management_version is None
    assert node.remote_management_accepted_at is None

    config = (await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == node_id))).scalar_one()
    assert config.version == 1
    assert config.rx_alt_ft == pytest.approx(100.0)
    assert config.tx_alt_ft is None
    assert (config.rx_lat, config.rx_lon, config.tx_lat, config.tx_lon) == (51.42, -0.91, 51.37, -0.88)
    assert (config.fc_hz, config.fs_hz) == (5.7e8, 2.0e6)
    assert config.beam_width_deg is None
    assert config.beam_azimuth_deg is None
    assert config.tx_callsign is None

    claim = await node_session.get(NodeClaim, node_id)
    assert (claim.user_id, claim.email, claim.verified) == ("user-a", "ada@example.com", True)

    radar = await node_session.get(PolledRadar, node_id)
    assert radar.epoch == 1
    assert (radar.scheme, radar.host, radar.port, radar.endpoint_key) == (
        "http",
        "radar.example.com",
        3000,
        "radar.example.com:3000",
    )
    assert radar.trust_state == "probation"
    assert radar.liveness == "pending"
    assert radar.consecutive_failures == 0
    assert radar.unprotected is True
    assert radar.config_fingerprint == FINGERPRINT
    assert radar.last_resolved_ip == "192.0.2.10"
    assert radar.auth_user is None
    assert radar.auth_secret_enc is None
    assert radar.last_frame_at is None
    assert radar.last_config_at is None
    assert radar.created_at is not None
    assert radar.resolved_at == STORED_NOW
    assert radar.probe_passed_at == STORED_NOW
    assert radar.endpoint_changed_at == STORED_NOW

    history = (
        (
            await node_session.execute(
                select(PolledRadarEndpointHistory).where(PolledRadarEndpointHistory.node_id == node_id)
            )
        )
        .scalars()
        .all()
    )
    assert [(h.old_key, h.new_key, h.resolved_ip, h.changed_by, h.changed_at) for h in history] == [
        (None, "radar.example.com:3000", "192.0.2.10", "user-a", STORED_NOW)
    ]


async def test_a_second_registration_of_the_endpoint_is_refused_whole(node_session):
    await create_polled_radar(node_session, **_args())
    await node_session.commit()

    with pytest.raises(EndpointAlreadyRegistered):
        await create_polled_radar(node_session, **_args(owner_user_id="user-b", owner_email="bob@example.com"))
    await node_session.commit()

    for model in (Node, NodeConfig, NodeClaim, PolledRadar, PolledRadarEndpointHistory):
        assert await _count(node_session, model) == 1, model.__tablename__


async def test_the_caller_transaction_survives_an_endpoint_clash(node_session):
    await create_polled_radar(node_session, **_args())
    await node_session.commit()

    await create_polled_radar(node_session, **_args(endpoint_key="other.example.com:3000", host="other.example.com"))
    with pytest.raises(EndpointAlreadyRegistered):
        await create_polled_radar(node_session, **_args())
    await node_session.commit()

    assert await _count(node_session, PolledRadar) == 2


async def test_the_secret_is_encrypted_at_rest(node_session, secret_key):
    registration = await create_polled_radar(node_session, **_args(auth_user="admin", auth_secret="hunter2"))
    await node_session.commit()

    stored = (
        await node_session.execute(
            text("SELECT auth_user, auth_secret_enc FROM polled_radars WHERE node_id = :id"),
            {"id": registration.node.node_id},
        )
    ).one()
    assert stored.auth_user == "admin"
    assert "hunter2" not in stored.auth_secret_enc
    assert poller_credentials(registration.radar) == ("admin", "hunter2")


async def test_a_radar_without_credentials_gives_the_poller_none(node_session):
    registration = await create_polled_radar(node_session, **_args())

    assert poller_credentials(registration.radar) is None


async def test_storing_a_secret_without_the_key_is_refused_before_anything_is_written(node_session, monkeypatch):
    monkeypatch.delenv("POLLED_RADAR_SECRET_KEY", raising=False)

    with pytest.raises(SecretKeyUnavailable):
        await create_polled_radar(node_session, **_args(auth_user="admin", auth_secret="hunter2"))
    await node_session.commit()

    assert await _count(node_session, Node) == 0


async def test_credentials_typed_into_the_address_are_not_kept(node_session, secret_key):
    registration = await create_polled_radar(
        node_session,
        **_args(endpoint_raw="http://admin:hunter2@radar.example.com:3000/", auth_user="admin", auth_secret="hunter2"),
    )

    assert registration.radar.endpoint_raw == "http://radar.example.com:3000/"


@pytest.mark.parametrize(
    ("typed", "kept"),
    [
        ("http://admin:p@ss@radar.example.com:3000", "http://radar.example.com:3000"),
        ("admin:p@ss@radar.example.com:3000", "radar.example.com:3000"),
        ("radar.example.com:3000", "radar.example.com:3000"),
        ("http://radar.example.com:3000/?next=a@b", "http://radar.example.com:3000/?next=a@b"),
    ],
)
async def test_no_piece_of_a_secret_survives_in_the_address(node_session, secret_key, typed, kept):
    registration = await create_polled_radar(node_session, **_args(endpoint_raw=typed))

    assert registration.radar.endpoint_raw == kept


def test_the_repr_names_neither_the_address_nor_the_secret():
    radar = PolledRadar(
        node_id="bla1a2b3c4d",
        endpoint_raw="http://radar.example.com:3000/",
        host="radar.example.com",
        endpoint_key="radar.example.com:3000",
        last_resolved_ip="192.0.2.10",
        auth_secret_enc="ciphertext",
    )
    history = PolledRadarEndpointHistory(
        node_id="bla1a2b3c4d", old_key="a.example.com:1", new_key="radar.example.com:3000", resolved_ip="192.0.2.10"
    )

    for shown in (repr(radar), repr(history)):
        assert "bla1a2b3c4d" in shown
        for secret in ("radar.example.com", "192.0.2.10", "ciphertext", "a.example.com"):
            assert secret not in shown


async def test_history_goes_with_the_registration(node_session):
    registration = await create_polled_radar(node_session, **_args())
    await node_session.commit()

    await node_session.execute(delete(PolledRadar).where(PolledRadar.node_id == registration.node.node_id))
    await node_session.commit()

    assert await _count(node_session, PolledRadarEndpointHistory) == 0


async def test_an_unknown_publication_is_refused(node_session):
    with pytest.raises(ValueError, match="publication"):
        await create_polled_radar(node_session, **_args(publication="secret"))


# ── A configuration from what the radar declares ──────────────────────────────

# A configuration held for a radar before it declares anything new: every field
# differs from STOCK_CONFIG's declaration, so the test sees which side won.
HELD = {
    "rx_lat": 51.42,
    "rx_lon": -0.91,
    "rx_alt_ft": 120.0,
    "tx_lat": 51.37,
    "tx_lon": -0.88,
    "tx_alt_ft": 900.0,
    "tx_callsign": "Crystal Palace",
    "fc_hz": 570_000_000.0,
    "fs_hz": 2_400_000.0,
    "beam_width_deg": 41.0,
    "beam_azimuth_deg": 90.0,
    "max_range_km": 50.0,
    "cpi_s": 0.5,
    "delay_tolerance_us": 6.67,
    "doppler_tolerance_hz": 5.0,
}


def test_a_probed_configuration_takes_what_the_radar_declares_and_keeps_the_rest():
    config = probed_config(parse_config(STOCK_CONFIG), HELD)

    assert config == HELD | {
        "rx_lat": 10.5,
        "rx_lon": -30.25,
        "rx_alt_ft": pytest.approx(12 / 0.3048),
        "tx_lat": 10.75,
        "tx_lon": -30.5,
        "tx_alt_ft": pytest.approx(300 / 0.3048),
        "fc_hz": 204_640_000.0,
        "fs_hz": 2_000_000.0,
        "cpi_s": 0.75,
    }


def test_fs_and_cpi_the_radar_does_not_declare_keep_the_values_held():
    stock = copy.deepcopy(STOCK_CONFIG)
    del stock["capture"]["fs"], stock["process"]["data"]["cpi"]

    config = probed_config(parse_config(stock), HELD)

    assert (config["fs_hz"], config["cpi_s"]) == (HELD["fs_hz"], HELD["cpi_s"])


# Conversion noise is relative to the value, so it is absorbed at sea level too.
@pytest.mark.parametrize("rx_alt_m", [12, 0.05, 0])
def test_a_declaration_within_float_noise_of_the_held_value_keeps_it(rx_alt_m):
    stock = copy.deepcopy(STOCK_CONFIG)
    stock["location"]["rx"]["altitude"] = rx_alt_m
    held = probed_config(parse_config(stock), HELD)
    noisy = held | {"rx_alt_ft": held["rx_alt_ft"] * (1 + 1e-12)}

    assert probed_config(parse_config(stock), held) == held
    assert probed_config(parse_config(stock), noisy) == noisy


def test_a_declaration_the_validator_refuses_is_refused():
    stock = copy.deepcopy(STOCK_CONFIG)
    stock["capture"]["fc"] = 500_000

    with pytest.raises(ConfigInvalid) as info:
        probed_config(parse_config(stock), HELD)
    assert info.value.field == "fc_hz"
