import copy
import re
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select, text, update

from core.node_ids import node_id_pattern
from core.nodes import Node, NodeClaim, NodeConfig, NodeEvent, PolledRadar, PolledRadarEndpointHistory
from core.secrets import SecretKeyUnavailable
from services.blah2_probe import Blah2Probe, parse_config
from services.node_config import ConfigInvalid
from services.node_config_store import active_config
from services.polled_endpoint import PolledEndpoint
from services.polled_radars import (
    EndpointAlreadyRegistered,
    EpochChanged,
    RadarGeometry,
    create_polled_radar,
    poller_credentials,
    probed_config,
    remove_polled_radar,
    set_polled_radar_address,
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


async def test_a_registration_under_no_licence_records_none(node_session):
    registration = await create_polled_radar(node_session, **_args(licence_version=None))
    await node_session.commit()
    node_id = registration.node.node_id
    node_session.expire_all()
    node = await node_session.get(Node, node_id)
    assert (node.licence_version, node.licence_accepted_at) == (None, None)


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


# ── A new address ────────────────────────────────────────────────────────────

LATER = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
STORED_LATER = LATER.replace(tzinfo=None)


def _found_at(endpoint_key: str, *, auth: tuple[str, str] | None = None, config: dict = STOCK_CONFIG) -> Blah2Probe:
    """What a probe of `endpoint_key` reports when a stock radar declaring `config` answers there."""
    host, port = endpoint_key.rsplit(":", 1)
    userinfo = f"{auth[0]}:{auth[1]}@" if auth else ""
    endpoint = PolledEndpoint(
        scheme="http",
        host=host,
        port=int(port),
        endpoint_key=endpoint_key,
        auth_user=auth[0] if auth else None,
        auth_secret=auth[1] if auth else None,
        raw=f"http://{userinfo}{endpoint_key}",
    )
    declared = parse_config(config)
    return Blah2Probe(
        endpoint=endpoint,
        rx=declared.rx,
        tx=declared.tx,
        fc_hz=declared.fc_hz,
        fs_hz=declared.fs_hz,
        cpi_s=declared.cpi_s,
        config_fingerprint=declared.fingerprint,
        address="192.0.2.20",
        clock_offset_s=0.0,
    )


async def _registered(session, **overrides) -> PolledRadar:
    """A radar registered and since graduated, streaming, with a failure or two behind it."""
    registration = await create_polled_radar(session, **_args(**overrides))
    registration.radar.trust_state = "graduated"
    registration.radar.liveness = "streaming"
    registration.radar.consecutive_failures = 2
    await session.commit()
    return registration.radar


async def _moved(session, radar, probe: Blah2Probe, *, unprotected: bool = True) -> bool:
    return await set_polled_radar_address(
        session, radar, probe, unprotected=unprotected, changed_by="user-a", now=LATER
    )


async def test_a_new_address_starts_an_epoch_on_probation(node_session):
    radar = await _registered(node_session)
    node_id = radar.node_id

    began = await _moved(node_session, radar, _found_at("other.example.com:4000"))
    await node_session.commit()
    node_session.expire_all()

    assert began is True
    row = await node_session.get(PolledRadar, node_id)
    assert (row.epoch, row.trust_state, row.liveness, row.consecutive_failures) == (2, "probation", "pending", 0)
    assert (row.endpoint_key, row.host, row.port) == ("other.example.com:4000", "other.example.com", 4000)
    assert row.endpoint_raw == "http://other.example.com:4000"
    assert (row.config_fingerprint, row.last_resolved_ip) == (parse_config(STOCK_CONFIG).fingerprint, "192.0.2.20")
    # Probed at the moment it moved, so the poller takes it up at once.
    assert row.endpoint_changed_at == row.probe_passed_at == row.resolved_at == row.last_config_at == STORED_LATER
    history = (
        await node_session.execute(select(PolledRadarEndpointHistory).order_by(PolledRadarEndpointHistory.id))
    ).scalars()
    assert [(h.old_key, h.new_key, h.resolved_ip, h.changed_by) for h in history] == [
        (None, "radar.example.com:3000", "192.0.2.10", "user-a"),
        ("radar.example.com:3000", "other.example.com:4000", "192.0.2.20", "user-a"),
    ]


async def test_what_the_radar_declares_at_its_new_address_is_its_configuration(node_session):
    radar = await _registered(node_session)
    node_id = radar.node_id

    await _moved(node_session, radar, _found_at("other.example.com:4000"))
    await node_session.commit()

    active = await active_config(node_session, node_id)
    assert (await node_session.get(Node, node_id)).active_config_version == active.version == 2
    assert (active.rx_lat, active.tx_lon, active.fc_hz, active.cpi_s) == (10.5, -30.5, 204_640_000.0, 0.75)
    # Nothing blah2 declares, so held from the version before.
    assert active.max_range_km == 150.0


async def test_the_same_address_with_a_new_password_keeps_its_epoch(node_session, secret_key):
    radar = await _registered(node_session)
    node_id = radar.node_id

    # The radar declares STOCK_CONFIG, not what its epoch holds: that is the
    # config poll's to find, not the owner's password change.
    began = await _moved(
        node_session, radar, _found_at("radar.example.com:3000", auth=("owner", "hunter2")), unprotected=False
    )
    await node_session.commit()
    node_session.expire_all()

    assert began is False
    row = await node_session.get(PolledRadar, node_id)
    assert (row.epoch, row.trust_state, row.unprotected, row.config_fingerprint) == (1, "graduated", False, FINGERPRINT)
    assert poller_credentials(row) == ("owner", "hunter2")
    assert "hunter2" not in row.endpoint_raw
    assert (row.endpoint_changed_at, row.probe_passed_at) == (STORED_NOW, STORED_NOW)
    assert (await node_session.get(Node, node_id)).active_config_version == 1
    assert await _count(node_session, PolledRadarEndpointHistory) == 1


async def test_a_password_left_out_of_the_new_address_is_forgotten(node_session, secret_key):
    radar = await _registered(node_session, auth_user="owner", auth_secret="hunter2", unprotected=False)
    node_id = radar.node_id

    await _moved(node_session, radar, _found_at("radar.example.com:3000"))
    await node_session.commit()
    node_session.expire_all()

    row = await node_session.get(PolledRadar, node_id)
    assert (row.auth_user, row.auth_secret_enc, row.unprotected) == (None, None, True)


async def test_an_address_another_radar_holds_is_refused_whole(node_session):
    radar = await _registered(node_session)
    node_id = radar.node_id
    await create_polled_radar(
        node_session,
        **_args(
            owner_user_id="user-b",
            owner_email="bob@example.com",
            endpoint_key="other.example.com:4000",
            host="other.example.com",
        ),
    )
    await node_session.commit()

    with pytest.raises(EndpointAlreadyRegistered):
        await _moved(node_session, radar, _found_at("other.example.com:4000"))
    await node_session.commit()
    node_session.expire_all()

    row = await node_session.get(PolledRadar, node_id)
    assert (row.epoch, row.endpoint_key, row.trust_state) == (1, "radar.example.com:3000", "graduated")
    assert await _count(node_session, NodeConfig) == 2
    assert await _count(node_session, PolledRadarEndpointHistory) == 2


async def test_an_epoch_the_poller_has_moved_on_is_left_as_it_is(node_session):
    radar = await _registered(node_session)
    node_id = radar.node_id
    # The poller starts epoch 2 in a session of its own while the owner's probe runs.
    await node_session.execute(
        update(PolledRadar).where(PolledRadar.node_id == node_id).values(epoch=2),
        execution_options={"synchronize_session": False},
    )
    await node_session.commit()

    with pytest.raises(EpochChanged):
        await _moved(node_session, radar, _found_at("other.example.com:4000"))
    await node_session.commit()
    node_session.expire_all()

    row = await node_session.get(PolledRadar, node_id)
    assert (row.epoch, row.endpoint_key) == (2, "radar.example.com:3000")
    assert await _count(node_session, NodeConfig) == 1
    assert await _count(node_session, PolledRadarEndpointHistory) == 1


# ── Removing a radar ─────────────────────────────────────────────────────────


async def test_removing_a_radar_retires_it_and_forgets_where_it_was(node_session, secret_key):
    radar = await _registered(node_session, auth_user="owner", auth_secret="hunter2")
    node_id = radar.node_id

    assert await remove_polled_radar(node_session, node_id, user_id="user-a") is True
    await node_session.commit()
    node_session.expire_all()

    assert (await node_session.get(Node, node_id)).status == "retired"
    for model in (PolledRadar, PolledRadarEndpointHistory, NodeClaim):
        assert await _count(node_session, model) == 0, model.__tablename__
    # The geometry its archived frames were filed against stays readable.
    assert await _count(node_session, NodeConfig) == 1
    [event] = (await node_session.execute(select(NodeEvent))).scalars()
    assert (event.node_id, event.kind, event.epoch, event.actor) == (node_id, "removed", 1, "user-a")


async def test_only_its_owner_removes_a_radar(node_session):
    radar = await _registered(node_session)
    node_id = radar.node_id

    assert await remove_polled_radar(node_session, node_id, user_id="user-b") is False
    await node_session.commit()
    node_session.expire_all()

    assert (await node_session.get(Node, node_id)).status == "active"
    for model in (PolledRadar, NodeClaim):
        assert await _count(node_session, model) == 1, model.__tablename__
    assert await _count(node_session, NodeEvent) == 0


async def test_a_removed_radars_address_registers_again_as_a_new_radar(node_session):
    radar = await _registered(node_session)
    node_id = radar.node_id
    await remove_polled_radar(node_session, node_id, user_id="user-a")
    await node_session.commit()

    again = await create_polled_radar(node_session, **_args(now=LATER))
    await node_session.commit()

    assert again.node.node_id != node_id
    assert (again.radar.epoch, again.radar.trust_state) == (1, "probation")


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
