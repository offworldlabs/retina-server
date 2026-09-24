"""An owner registering a stock blah2 radar from a signed-in session.

Whether a registration may happen, and what it records: the probe of what the
owner typed, the check that the radar still declares what the owner confirmed,
and the limits on how often a caller may make our server probe a host of its
choosing. services/polled_radars.py writes the rows.

Nothing is held between the owner's probe and their submit. The submit probes
again, so the rows hold what the radar declared at the moment they were written,
and a radar whose declaration moved in between is shown to the owner again
rather than registered on a look it no longer matches.
"""

import dataclasses
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.constants import YAGI_MAX_RANGE_KM
from core import secrets
from core.nodes import NodeClaim, PolledRadar
from services.blah2_probe import CONFIG_MAX_BYTES, CONFIG_PATH, Blah2Probe, Site, probe_blah2
from services.node_config import ConfigInvalid
from services.node_rate_limits import polled_probe_limiter
from services.polled_endpoint import (
    AddressPolicy,
    EndpointRefused,
    PolledEndpoint,
    Resolver,
    parse_endpoint,
    pinned_client,
    resolve_host,
)
from services.polled_radars import (
    EndpointAlreadyRegistered,
    PolledRegistration,
    RadarGeometry,
    create_polled_radar,
    probed_config,
)

ENABLED_ENV = "POLLED_RADAR_REGISTRATION_ENABLED"

MAX_RADARS_PER_ACCOUNT = 10
MAX_PROBES_IN_FLIGHT = 4
BUSY_RETRY_AFTER_S = 5

# What stock blah2 leaves out, as the fleet's own nodes default it (retina-node
# config/default.yml), and the server's own range for a Yagi.
REGISTRATION_BASE: dict[str, Any] = {
    "fs_hz": 2_000_000.0,
    "cpi_s": 0.5,
    "delay_tolerance_us": 2.0,
    "doppler_tolerance_hz": 5.0,
    "max_range_km": YAGI_MAX_RANGE_KM,
    "beam_width_deg": None,
    "beam_azimuth_deg": None,
    "tx_callsign": None,
}

# Read at call time, so a test can point the probe at a stub on loopback.
resolver: Resolver = resolve_host
policy: AddressPolicy | None = None

# Probes running now, across every caller. Checked and counted with no await in
# between, so on the one event loop it needs no lock.
_in_flight = 0


def enabled() -> bool:
    """Whether this deployment takes registrations. Exactly `1`, nothing else."""
    return os.environ.get(ENABLED_ENV, "") == "1"


class RegistrationRefused(Exception):
    """A registration turned away, with a message fit for the owner."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after_s: int | None = None,
        probe: "Probed | None" = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after_s = retry_after_s
        self.probe = probe


@dataclass(frozen=True)
class Probed:
    endpoint: PolledEndpoint
    radar: Blah2Probe
    # validate_config's shape, altitudes in feet.
    config: dict[str, Any]
    # Credentials were given and the radar refused a read without them.
    protected: bool

    def public(self) -> dict[str, Any]:
        """What the owner is shown. Never the credentials."""
        host = f"[{self.endpoint.host}]" if ":" in self.endpoint.host else self.endpoint.host
        return {
            "address": f"{self.endpoint.scheme}://{host}:{self.endpoint.port}",
            "rx": _site(self.radar.rx),
            "tx": _site(self.radar.tx),
            "fc_hz": self.radar.fc_hz,
            # As registered: blah2's own values, or the defaults where it gave none.
            "fs_hz": self.config["fs_hz"],
            "cpi_s": self.config["cpi_s"],
            "fingerprint": self.radar.config_fingerprint,
            "protected": self.protected,
        }


def _site(site: Site) -> dict[str, Any]:
    return {"latitude": site.latitude, "longitude": site.longitude, "altitude_m": site.altitude_m, "name": site.name}


def _unprocessable(exc: EndpointRefused) -> RegistrationRefused:
    return RegistrationRefused(422, str(exc.code), exc.message)


async def _answers_without_credentials(endpoint: PolledEndpoint) -> bool:
    bare = dataclasses.replace(endpoint, auth_user=None, auth_secret=None)
    try:
        async with pinned_client(bare, resolver=resolver, policy=policy) as client:
            await client.get(CONFIG_PATH, max_bytes=CONFIG_MAX_BYTES)
    except EndpointRefused:
        # Refused, or not answering at all: nothing seen open.
        return False
    return True


async def probe(session: AsyncSession, raw: str, user_id: str) -> Probed:
    """Probe what the owner typed and report what the radar declares, or raise
    RegistrationRefused. Saves nothing.

    Every refusal that needs no request is made before one is sent, and all but
    the registered-address check before the owner's allowance is spent.
    """
    global _in_flight
    try:
        endpoint = parse_endpoint(raw)
    except EndpointRefused as exc:
        raise _unprocessable(exc) from None
    if endpoint.auth_secret is not None:
        try:
            secrets.require_key()
        except secrets.SecretKeyUnavailable:
            raise RegistrationRefused(
                503,
                "credentials_unavailable",
                "This server cannot store a radar password yet. Register without one, or try again later.",
            ) from None
    if _in_flight >= MAX_PROBES_IN_FLIGHT:
        raise RegistrationRefused(
            429, "busy", "The server is checking other radars. Try again shortly.", retry_after_s=BUSY_RETRY_AFTER_S
        )
    # Taken with no await since the check above, and held until the probe ends.
    _in_flight += 1
    try:
        refusal = polled_probe_limiter.admit(user_id)
        if refusal is not None:
            raise RegistrationRefused(
                429,
                "rate_limited",
                "You have checked radars too often. Try again later.",
                retry_after_s=refusal.retry_after_s,
            )
        # After the allowance is spent: the answer says whether an address is a
        # registered radar, so asking is metered like a probe.
        taken = await session.scalar(
            select(PolledRadar.node_id).where(PolledRadar.endpoint_key == endpoint.endpoint_key)
        )
        if taken is not None:
            raise RegistrationRefused(409, "endpoint_registered", "This radar is already registered.")
        radar = await probe_blah2(endpoint, resolver=resolver, policy=policy)
        protected = endpoint.auth_user is not None and not await _answers_without_credentials(endpoint)
    except EndpointRefused as exc:
        raise _unprocessable(exc) from None
    finally:
        _in_flight -= 1

    try:
        config = probed_config(radar.config, REGISTRATION_BASE)
    except ConfigInvalid as exc:
        raise RegistrationRefused(
            422,
            "config_invalid",
            f"The radar's configuration holds a value the network cannot use ({exc.field}: {exc.reason}); "
            "correct it in blah2's config.",
        ) from None
    return Probed(endpoint=endpoint, radar=radar, config=config, protected=protected)


async def register(
    session: AsyncSession, *, raw: str, fingerprint: str, publication: str, user: dict
) -> PolledRegistration:
    """Probe again and write the radar's rows under `user`, or raise
    RegistrationRefused. The caller commits.

    `fingerprint` is the declaration the owner confirmed. A radar declaring
    anything else now is refused with what it declares, for the owner to look
    at again.
    """
    owned = await session.scalar(
        select(func.count())
        .select_from(PolledRadar)
        .join(NodeClaim, NodeClaim.node_id == PolledRadar.node_id)
        .where(NodeClaim.user_id == user["id"])
    )
    if owned >= MAX_RADARS_PER_ACCOUNT:
        raise RegistrationRefused(
            403, "radar_limit", f"An account can register at most {MAX_RADARS_PER_ACCOUNT} radars."
        )
    probed = await probe(session, raw, user["id"])
    radar, endpoint, config = probed.radar, probed.endpoint, probed.config
    if radar.config_fingerprint != fingerprint:
        raise RegistrationRefused(
            409,
            "config_changed",
            "The radar's configuration has changed since you checked it. Confirm what it declares now.",
            probe=probed,
        )
    try:
        return await create_polled_radar(
            session,
            owner_user_id=user["id"],
            owner_email=user["email"],
            endpoint_raw=endpoint.raw,
            scheme=endpoint.scheme,
            host=endpoint.host,
            port=endpoint.port,
            endpoint_key=endpoint.endpoint_key,
            auth_user=endpoint.auth_user,
            auth_secret=endpoint.auth_secret,
            geometry=RadarGeometry(
                rx_lat=radar.rx.latitude,
                rx_lon=radar.rx.longitude,
                rx_alt_m=radar.rx.altitude_m,
                tx_lat=radar.tx.latitude,
                tx_lon=radar.tx.longitude,
                tx_alt_m=radar.tx.altitude_m,
            ),
            fc_hz=config["fc_hz"],
            fs_hz=config["fs_hz"],
            max_range_km=config["max_range_km"],
            cpi_s=config["cpi_s"],
            delay_tolerance_us=config["delay_tolerance_us"],
            doppler_tolerance_hz=config["doppler_tolerance_hz"],
            config_fingerprint=radar.config_fingerprint,
            resolved_ip=radar.address,
            publication=publication,
            licence_version=None,
            unprotected=not probed.protected,
        )
    except EndpointAlreadyRegistered:
        raise RegistrationRefused(409, "endpoint_registered", "This radar is already registered.") from None
