"""Registering a polled stock-blah2 radar as a node, moving it and removing it.

A polled radar is a `nodes` row, a configuration version, a claim and one
`polled_radars` row, so the pipeline and the ownership code need nothing of
their own for it. Nothing here commits, as in services/node_auth.py: the route
owns the transaction, and invalidates the publication cache after committing.

The endpoint arrives already split into parts by the caller's normaliser; this
module stores them and does not parse URLs.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core import secrets
from core.node_ids import POLLED_BLAH2, add_with_minted_id
from core.nodes import Node, NodeClaim, NodeConfig, NodeEvent, PolledRadar, PolledRadarEndpointHistory
from services.blah2_probe import Blah2Config, Blah2Probe
from services.node_auth import mint_node_ref
from services.node_claim_store import clear_claim, read_owner
from services.node_config import validate_config
from services.node_config_store import active_config, config_fields, upsert_config

FEET_PER_METRE = 1 / 0.3048

PUBLICATIONS = ("public", "private")

# The `user:secret@` of an authority, with or without a scheme in front. Up to
# the last `@`, as URL parsers split it, or a secret holding one leaves a piece.
_USERINFO = re.compile(r"^((?:[A-Za-z][A-Za-z0-9+.-]*:)?//)?[^/?#]*@")


class EndpointAlreadyRegistered(Exception):
    """Another polled radar already holds this endpoint key."""


class EpochChanged(Exception):
    """The radar is no longer at the epoch the change was made against."""


@dataclass(frozen=True)
class RadarGeometry:
    """The owner-confirmed positions, altitudes in metres as stock blah2 gives them."""

    rx_lat: float | None
    rx_lon: float | None
    rx_alt_m: float | None
    tx_lat: float | None
    tx_lon: float | None
    tx_alt_m: float | None


@dataclass(frozen=True)
class PolledRegistration:
    node: Node
    radar: PolledRadar


def _feet(metres: float | None) -> float | None:
    return None if metres is None else metres * FEET_PER_METRE


def _without_userinfo(raw: str) -> str:
    return _USERINFO.sub(lambda match: match.group(1) or "", raw.strip(), count=1)


def _same(held: Any, declared: float) -> bool:
    # Relative, so feet converted from the same metres by another path still match.
    return isinstance(held, int | float) and math.isclose(held, declared, rel_tol=1e-9)


def probed_config(config: Blah2Config, base: Mapping[str, Any]) -> dict[str, Any]:
    """A configuration version holding what the radar declares, laid over `base`.

    `base` is a whole configuration in validate_config's shape. blah2 declares
    its two sites and fc, and fs and CPI where its config has them; the rest,
    fs or CPI it leaves out included, is carried over from `base`. A declared
    value within float noise of `base`'s keeps `base`'s, so an unchanged radar
    reads back equal and upsert_config writes nothing. Raises ConfigInvalid as
    validate_config does.
    """
    declared = {
        "rx_lat": config.rx.latitude,
        "rx_lon": config.rx.longitude,
        "rx_alt_ft": _feet(config.rx.altitude_m),
        "tx_lat": config.tx.latitude,
        "tx_lon": config.tx.longitude,
        "tx_alt_ft": _feet(config.tx.altitude_m),
        "fc_hz": config.fc_hz,
        "fs_hz": config.fs_hz,
        "cpi_s": config.cpi_s,
    }
    merged = dict(base)
    for field, value in declared.items():
        if value is not None and not _same(base.get(field), value):
            merged[field] = value
    return validate_config(merged)


async def create_polled_radar(
    session: AsyncSession,
    *,
    owner_user_id: str,
    owner_email: str,
    endpoint_raw: str,
    scheme: str,
    host: str,
    port: int,
    endpoint_key: str,
    auth_user: str | None,
    auth_secret: str | None,
    geometry: RadarGeometry,
    fc_hz: float,
    fs_hz: float,
    max_range_km: float,
    cpi_s: float,
    delay_tolerance_us: float,
    doppler_tolerance_hz: float,
    config_fingerprint: str,
    resolved_ip: str | None,
    publication: str,
    licence_version: str | None,
    unprotected: bool,
    now: datetime | None = None,
) -> PolledRegistration:
    """Write the node, its first configuration, its claim, the polling row and
    the first endpoint history entry, and flush. The caller commits.

    All or nothing within the caller's transaction: an endpoint another radar
    holds raises EndpointAlreadyRegistered with none of the five rows left
    behind. A secret without a usable key raises SecretKeyUnavailable before
    anything is written. A registration under no licence leaves both licence
    columns null.
    """
    if publication not in PUBLICATIONS:
        raise ValueError(f"publication must be one of {PUBLICATIONS}, not {publication!r}")
    now = now or datetime.now(UTC)
    secret_enc = None if auth_secret is None else secrets.encrypt(auth_secret)

    def build(node_id: str) -> Node:
        return Node(
            node_id=node_id,
            node_ref=mint_node_ref(),
            board_model="blah2",
            status="active",
            active_config_version=1,
            licence_version=licence_version,
            licence_accepted_at=now if licence_version is not None else None,
            publication=publication,
            publication_chosen_at=now,
            first_seen_at=now,
        )

    async with session.begin_nested():
        node = await add_with_minted_id(session, POLLED_BLAH2, build)
        # Outside the minting savepoint: an endpoint clash retried as an id
        # clash would fail every attempt alike and surface as the wrong error.
        session.add(
            NodeConfig(
                node_id=node.node_id,
                version=1,
                rx_lat=geometry.rx_lat,
                rx_lon=geometry.rx_lon,
                rx_alt_ft=_feet(geometry.rx_alt_m),
                tx_lat=geometry.tx_lat,
                tx_lon=geometry.tx_lon,
                tx_alt_ft=_feet(geometry.tx_alt_m),
                fc_hz=fc_hz,
                fs_hz=fs_hz,
                max_range_km=max_range_km,
                cpi_s=cpi_s,
                delay_tolerance_us=delay_tolerance_us,
                doppler_tolerance_hz=doppler_tolerance_hz,
                created_at=now,
            )
        )
        # Registered from a signed-in session, whose address sign-in confirmed.
        session.add(
            NodeClaim(
                node_id=node.node_id,
                user_id=owner_user_id,
                email=owner_email,
                verified=True,
                undeliverable=False,
                updated_at=now,
            )
        )
        radar = PolledRadar(
            node_id=node.node_id,
            epoch=1,
            endpoint_raw=_without_userinfo(endpoint_raw),
            scheme=scheme,
            host=host,
            port=port,
            endpoint_key=endpoint_key,
            auth_user=auth_user,
            auth_secret_enc=secret_enc,
            last_resolved_ip=resolved_ip,
            resolved_at=now if resolved_ip is not None else None,
            config_fingerprint=config_fingerprint,
            probe_passed_at=now,
            endpoint_changed_at=now,
            trust_state="probation",
            unprotected=unprotected,
            liveness="pending",
            consecutive_failures=0,
            created_at=now,
        )
        session.add(radar)
        try:
            await session.flush()
        except IntegrityError as exc:
            # `from None`: the chained error carries the statement's parameters,
            # the address among them, into any traceback that gets logged.
            if "endpoint_key" in str(exc.orig):
                raise EndpointAlreadyRegistered("this endpoint is already registered") from None
            raise
        session.add(
            PolledRadarEndpointHistory(
                node_id=node.node_id,
                old_key=None,
                new_key=endpoint_key,
                resolved_ip=resolved_ip,
                changed_by=owner_user_id,
                changed_at=now,
            )
        )
        await session.flush()
    return PolledRegistration(node=node, radar=radar)


async def set_polled_radar_address(
    session: AsyncSession,
    radar: PolledRadar,
    probe: Blah2Probe,
    *,
    unprotected: bool,
    changed_by: str,
    now: datetime | None = None,
) -> bool:
    """Point `radar` at the endpoint `probe` found it at, and flush. The caller
    commits. Returns whether a new epoch began.

    Another host or port starts one, on probation, holding what the radar
    declares there: whatever answers may be another box. The same host and port
    changes only how the radar is reached, and leaves what it declares to the
    config poll.

    Applied only at the epoch `radar` was read at. EpochChanged when the poller
    has moved it on since, and EndpointAlreadyRegistered for an endpoint another
    radar holds, leave every row as it was. A secret without a usable key raises
    SecretKeyUnavailable before anything is written.
    """
    now = now or datetime.now(UTC)
    endpoint = probe.endpoint
    # Read before the write, which leaves `radar` as it was read.
    node_id, epoch, old_key = radar.node_id, radar.epoch, radar.endpoint_key
    values: dict[str, Any] = {
        "endpoint_raw": _without_userinfo(endpoint.raw),
        "scheme": endpoint.scheme,
        "host": endpoint.host,
        "port": endpoint.port,
        "auth_user": endpoint.auth_user,
        "auth_secret_enc": None if endpoint.auth_secret is None else secrets.encrypt(endpoint.auth_secret),
        "unprotected": unprotected,
    }
    moved = endpoint.endpoint_key != old_key
    if moved:
        values |= {
            "epoch": epoch + 1,
            "endpoint_key": endpoint.endpoint_key,
            "trust_state": "probation",
            "config_fingerprint": probe.config_fingerprint,
            "probe_passed_at": now,
            "endpoint_changed_at": now,
            "last_resolved_ip": probe.address,
            "resolved_at": now,
            "last_config_at": now,
            "liveness": "pending",
            "consecutive_failures": 0,
        }
    async with session.begin_nested():
        try:
            written = await session.execute(
                update(PolledRadar)
                .where(PolledRadar.node_id == node_id, PolledRadar.epoch == epoch)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
        except IntegrityError as exc:
            # `from None` for the reason create_polled_radar gives.
            if "endpoint_key" in str(exc.orig):
                raise EndpointAlreadyRegistered("this endpoint is already registered") from None
            raise
        if written.rowcount != 1:
            raise EpochChanged(f"{node_id} is no longer at epoch {epoch}")
        if moved:
            active = await active_config(session, node_id)
            version = await upsert_config(session, node_id, probed_config(probe.config, config_fields(active)))
            await session.execute(update(Node).where(Node.node_id == node_id).values(active_config_version=version))
            session.add(
                PolledRadarEndpointHistory(
                    node_id=node_id,
                    old_key=old_key,
                    new_key=endpoint.endpoint_key,
                    resolved_ip=probe.address,
                    changed_by=changed_by,
                    changed_at=now,
                )
            )
            await session.flush()
    return moved


async def remove_polled_radar(session: AsyncSession, node_id: str, *, user_id: str) -> bool:
    """Retire `user_id`'s radar and forget where it was, and flush. The caller commits.

    Nothing can claim a polled radar once it is released, and the poller polls
    every active one, so handing it back would leave it polled with nobody to
    answer for it. The node stays, retired, with the configurations its archived
    frames were filed against and the record of decisions about it. Its
    address, stored password and address history go, so the address registers
    again as a new radar. False, changing nothing, for a radar `user_id` does
    not own.
    """
    if await read_owner(session, node_id) != user_id:
        return False
    radar = await session.get(PolledRadar, node_id)
    await clear_claim(session, node_id)
    if radar is not None:
        await session.delete(radar)
    await session.execute(update(Node).where(Node.node_id == node_id).values(status="retired"))
    session.add(
        NodeEvent(node_id=node_id, kind="removed", epoch=radar.epoch if radar is not None else None, actor=user_id)
    )
    await session.flush()
    return True


async def owner_views(session: AsyncSession, node_ids: list[str]) -> dict[str, dict]:
    """What an owner is shown of each of these that is a polled radar, keyed by node.

    The address as they gave it, which only they and administrators see, less
    the password, which nobody does.
    """
    if not node_ids:
        return {}
    radars = await session.scalars(select(PolledRadar).where(PolledRadar.node_id.in_(node_ids)))
    return {
        radar.node_id: {
            "address": radar.endpoint_raw,
            "unprotected": radar.unprotected,
            "liveness": radar.liveness,
            "trust_state": radar.trust_state,
        }
        for radar in radars
    }


def poller_credentials(radar: PolledRadar) -> tuple[str, str] | None:
    """The username and plaintext secret the poller presents, or None.

    The only reader of the plaintext. Anything shown to a person reports at
    most whether a credential is held.
    """
    if radar.auth_secret_enc is None:
        return None
    return radar.auth_user or "", secrets.decrypt(radar.auth_secret_enc)
