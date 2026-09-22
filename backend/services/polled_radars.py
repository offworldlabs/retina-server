"""Registering a polled stock-blah2 radar as a node.

A polled radar is a `nodes` row, a configuration version, a claim and one
`polled_radars` row, so the pipeline and the ownership code need nothing of
their own for it. Nothing here commits, as in services/node_auth.py: the route
owns the transaction, and invalidates the publication cache after committing.

The endpoint arrives already split into parts by the caller's normaliser; this
module stores them and does not parse URLs.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core import secrets
from core.node_ids import POLLED_BLAH2, add_with_minted_id
from core.nodes import Node, NodeClaim, NodeConfig, PolledRadar, PolledRadarEndpointHistory
from services.node_auth import mint_node_ref

FEET_PER_METRE = 1 / 0.3048

PUBLICATIONS = ("public", "private")

# The `user:secret@` of an authority, with or without a scheme in front. Up to
# the last `@`, as URL parsers split it, or a secret holding one leaves a piece.
_USERINFO = re.compile(r"^((?:[A-Za-z][A-Za-z0-9+.-]*:)?//)?[^/?#]*@")


class EndpointAlreadyRegistered(Exception):
    """Another polled radar already holds this endpoint key."""


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
    licence_version: str,
    unprotected: bool,
    now: datetime | None = None,
) -> PolledRegistration:
    """Write the node, its first configuration, its claim, the polling row and
    the first endpoint history entry, and flush. The caller commits.

    All or nothing within the caller's transaction: an endpoint another radar
    holds raises EndpointAlreadyRegistered with none of the five rows left
    behind. A secret without a usable key raises SecretKeyUnavailable before
    anything is written.
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
            licence_accepted_at=now,
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


def poller_credentials(radar: PolledRadar) -> tuple[str, str] | None:
    """The username and plaintext secret the poller presents, or None.

    The only reader of the plaintext. Anything shown to a person reports at
    most whether a credential is held.
    """
    if radar.auth_secret_enc is None:
        return None
    return radar.auth_user or "", secrets.decrypt(radar.auth_secret_enc)
