"""Whether an operator's endpoint is a live, stock 30hours/blah2 radar.

The probe asks for exactly two paths, CONFIG_PATH and DETECTION_PATH. Stock
blah2 answers side-effecting GETs such as /capture/toggle beside its read-only
ones, so nothing else is ever requested of a radar.

Connections go through services/polled_endpoint.py, which vets and pins the
address; this module only judges what the radar says.
"""

import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass
from enum import StrEnum

import httpx

from services.polled_endpoint import (
    AddressPolicy,
    EndpointRefused,
    PolledEndpoint,
    Refusal,
    Resolver,
    pinned_client,
    resolve_host,
)

CONFIG_PATH = "/api/config"
DETECTION_PATH = "/api/detection"
CONFIG_MAX_BYTES = 64 * 1024
DETECTION_MAX_BYTES = 256 * 1024

# Between the two detection reads: two of the radar's CPIs, so a live radar has
# emitted a frame in between, within bounds the probe deadline can hold.
MIN_FRESHNESS_GAP_S = 1.75
MAX_FRESHNESS_GAP_S = 5.0
MAX_CLOCK_OFFSET_S = 10.0
PROBE_DEADLINE_S = 15.0

# Where the example-configuration refusal points the blah2 author.
CONTACT_ROUTE = "hello@offworldlabs.com"

# The sites every stock config ships with, as (latitude, longitude, altitude m).
_EXAMPLE_RX = (-34.9286, 138.5999, 50.0)
_EXAMPLE_TX = (-34.9810, 138.7081, 750.0)
_EXAMPLE_TOLERANCE_DEG = 1e-4
_EXAMPLE_TOLERANCE_M = 1.0

# The API server appends each frame to a variable first initialised as
# undefined, so the first frame after it restarts arrives with this in front.
_UNDEFINED_PREFIX = b"undefined"


class Blah2Refusal(StrEnum):
    """Why a reachable endpoint was refused as a blah2 radar."""

    BLAH2_ARM = "blah2_arm"
    NOT_BLAH2 = "not_blah2"
    EXAMPLE_CONFIG = "example_config"
    NO_DETECTION_YET = "no_detection_yet"
    STALLED = "stalled"
    CLOCK_OFFSET = "clock_offset"


@dataclass(frozen=True)
class Site:
    latitude: float
    longitude: float
    altitude_m: float
    name: str | None


@dataclass(frozen=True)
class Blah2Config:
    rx: Site
    tx: Site
    fc_hz: float
    fs_hz: float | None
    cpi_s: float | None

    @property
    def fingerprint(self) -> str:
        """config_fingerprint of the body this was parsed from."""
        return _fingerprint(self)


@dataclass(frozen=True)
class DetectionFrame:
    # POSIX milliseconds on the radar's own clock.
    timestamp_ms: int
    delay_km: tuple[float, ...]
    doppler_hz: tuple[float, ...]
    snr_db: tuple[float, ...]


@dataclass(frozen=True)
class Blah2Probe:
    endpoint: PolledEndpoint
    rx: Site
    tx: Site
    fc_hz: float
    fs_hz: float | None
    cpi_s: float | None
    config_fingerprint: str
    # The address that answered: evidence of where the radar was, not identity.
    address: str
    # Radar clock minus server clock at the latest frame, the frame's age included.
    clock_offset_s: float

    @property
    def config(self) -> Blah2Config:
        """What the radar declared, as parse_config gave it."""
        return Blah2Config(self.rx, self.tx, self.fc_hz, self.fs_hz, self.cpi_s)


def _not_blah2(path: str, reason: str) -> EndpointRefused:
    return EndpointRefused(Blah2Refusal.NOT_BLAH2, f"{path} did not answer as stock blah2 does: {reason}.")


def _number(value: object) -> float | None:
    """A finite JSON number as a float, or None. Booleans are not numbers."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _positive(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _json(body: bytes, path: str) -> object:
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise _not_blah2(path, "the body is not JSON") from None


# ── /api/config ───────────────────────────────────────────────────────────────


def _site(location: dict, key: str) -> Site:
    site = location.get(key)
    if not isinstance(site, dict):
        raise _not_blah2(CONFIG_PATH, f"location.{key} is missing")
    latitude = _number(site.get("latitude"))
    longitude = _number(site.get("longitude"))
    altitude = _number(site.get("altitude"))
    if latitude is None or not -90 <= latitude <= 90:
        raise _not_blah2(CONFIG_PATH, f"location.{key}.latitude is not a number from -90 to 90")
    if longitude is None or not -180 <= longitude <= 180:
        raise _not_blah2(CONFIG_PATH, f"location.{key}.longitude is not a number from -180 to 180")
    if altitude is None:
        raise _not_blah2(CONFIG_PATH, f"location.{key}.altitude is not a number")
    name = site.get("name")
    return Site(latitude, longitude, altitude, name if isinstance(name, str) else None)


def _is_blah2_arm(payload: dict) -> bool:
    # blah2-arm narrows /api/config to {"truth": {"adsb": {"enabled": ...}}}.
    truth = payload.get("truth")
    adsb = truth.get("adsb") if isinstance(truth, dict) else None
    return payload.keys() == {"truth"} and isinstance(adsb, dict) and "enabled" in adsb


def _is_example(site: Site, example: tuple[float, float, float]) -> bool:
    latitude, longitude, altitude = example
    return (
        abs(site.latitude - latitude) <= _EXAMPLE_TOLERANCE_DEG
        and abs(site.longitude - longitude) <= _EXAMPLE_TOLERANCE_DEG
        and abs(site.altitude_m - altitude) <= _EXAMPLE_TOLERANCE_M
    )


def parse_config(payload: object) -> Blah2Config:
    """The parts of a decoded /api/config body the network uses.

    Raises EndpointRefused for a body that is not stock blah2's, for
    blah2-arm's, and for the shipped example sites.
    """
    if not isinstance(payload, dict):
        raise _not_blah2(CONFIG_PATH, "the body is not a JSON object")
    if _is_blah2_arm(payload):
        raise EndpointRefused(
            Blah2Refusal.BLAH2_ARM,
            "This is a RETINA node running blah2-arm. Those join the network through the RETINA node route, "
            "not by URL.",
        )
    location = payload.get("location")
    if not isinstance(location, dict):
        raise _not_blah2(CONFIG_PATH, "location is missing")
    rx, tx = _site(location, "rx"), _site(location, "tx")
    capture = payload.get("capture")
    fc = _number(capture.get("fc")) if isinstance(capture, dict) else None
    if fc is None or fc <= 0:
        raise _not_blah2(CONFIG_PATH, "capture.fc is not a positive number")
    fs = _positive(capture.get("fs"))
    process = payload.get("process")
    data = process.get("data") if isinstance(process, dict) else None
    cpi = _positive(data.get("cpi")) if isinstance(data, dict) else None
    if _is_example(rx, _EXAMPLE_RX) and _is_example(tx, _EXAMPLE_TX):
        raise EndpointRefused(
            Blah2Refusal.EXAMPLE_CONFIG,
            "This radar still has blah2's example configuration (receiver in Adelaide, transmitter on "
            "Mount Lofty). Set location.rx and location.tx to your own sites. If you are the blah2 author "
            f"and want to join the network, message us: {CONTACT_ROUTE}",
        )
    return Blah2Config(rx, tx, fc, fs, cpi)


def read_config(body: bytes) -> Blah2Config:
    """One /api/config body, judged as parse_config judges its decoded form."""
    return parse_config(_json(body, CONFIG_PATH))


def _fingerprint(config: Blah2Config) -> str:
    def site(s: Site) -> dict:
        # Adding 0.0 turns -0.0 into 0.0; floats make 50 and 50.0 one value.
        return {
            "latitude": s.latitude + 0.0,
            "longitude": s.longitude + 0.0,
            "altitude": s.altitude_m + 0.0,
            "name": s.name,
        }

    canonical = json.dumps(
        {"rx": site(config.rx), "tx": site(config.tx), "fc": config.fc_hz + 0.0},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def config_fingerprint(payload: object) -> str:
    """A hex digest of the sites and centre frequency in a decoded /api/config.

    Nothing else in the config moves it, so a change means the radar has moved
    or retuned. Refuses as parse_config does.
    """
    return _fingerprint(parse_config(payload))


# ── /api/detection ────────────────────────────────────────────────────────────


def parse_detection(body: bytes) -> DetectionFrame:
    """One /api/detection body. An empty frame, with no detections, is valid."""
    if not body.strip():
        # What blah2 serves until its first processing interval completes.
        raise EndpointRefused(
            Blah2Refusal.NO_DETECTION_YET,
            "blah2 is running but has not produced a detection yet; check it is capturing.",
        )
    if body.startswith(_UNDEFINED_PREFIX + b"{"):
        body = body[len(_UNDEFINED_PREFIX) :]
    payload = _json(body, DETECTION_PATH)
    if not isinstance(payload, dict):
        raise _not_blah2(DETECTION_PATH, "the body is not a JSON object")
    timestamp = payload.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
        raise _not_blah2(DETECTION_PATH, "timestamp is not a positive whole number of milliseconds")
    arrays = []
    for key in ("delay", "doppler", "snr"):
        values = payload.get(key)
        if not isinstance(values, list):
            raise _not_blah2(DETECTION_PATH, f"{key} is not a list")
        numbers = tuple(_number(v) for v in values)
        if any(n is None for n in numbers):
            raise _not_blah2(DETECTION_PATH, f"{key} holds something other than numbers")
        arrays.append(numbers)
    if len({len(a) for a in arrays}) != 1:
        raise _not_blah2(DETECTION_PATH, "delay, doppler and snr differ in length")
    return DetectionFrame(timestamp, *arrays)


# ── The probe ─────────────────────────────────────────────────────────────────


def freshness_gap_s(cpi_s: float | None) -> float:
    """How long to wait between the two detection reads."""
    if cpi_s is None:
        return MIN_FRESHNESS_GAP_S
    return min(max(MIN_FRESHNESS_GAP_S, 2 * cpi_s), MAX_FRESHNESS_GAP_S)


async def probe_blah2(
    endpoint: PolledEndpoint,
    *,
    resolver: Resolver = resolve_host,
    policy: AddressPolicy | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Blah2Probe:
    """Judge an endpoint as a live stock blah2, or raise EndpointRefused.

    Two detection reads freshness_gap_s apart: an idle radar serves its last
    frame forever, so one read cannot tell live from stalled.
    """
    try:
        async with asyncio.timeout(PROBE_DEADLINE_S):
            async with pinned_client(endpoint, resolver=resolver, policy=policy, transport=transport) as client:
                config = read_config(await client.get(CONFIG_PATH, max_bytes=CONFIG_MAX_BYTES))
                first = parse_detection(await client.get(DETECTION_PATH, max_bytes=DETECTION_MAX_BYTES))
                gap_s = freshness_gap_s(config.cpi_s)
                await asyncio.sleep(gap_s)
                latest = parse_detection(await client.get(DETECTION_PATH, max_bytes=DETECTION_MAX_BYTES))
                received = time.time()
                address = client.address
    except TimeoutError:
        raise EndpointRefused(Refusal.TIMED_OUT, f"{endpoint.host} did not finish answering in time.") from None

    if latest.timestamp_ms <= first.timestamp_ms:
        raise EndpointRefused(
            Blah2Refusal.STALLED,
            f"The radar's detection timestamp did not advance over {gap_s:g} s, so blah2 is not "
            "processing new data; check it is capturing.",
        )
    offset_s = latest.timestamp_ms / 1000 - received
    if abs(offset_s) > MAX_CLOCK_OFFSET_S:
        direction = "behind" if offset_s < 0 else "ahead of"
        raise EndpointRefused(
            Blah2Refusal.CLOCK_OFFSET,
            f"The radar's latest detection is stamped {abs(offset_s):.0f} s {direction} the server's clock, "
            f"beyond the {MAX_CLOCK_OFFSET_S:g} s allowed; check the radar's clock and that NTP is running on it.",
        )
    return Blah2Probe(
        endpoint=endpoint,
        rx=config.rx,
        tx=config.tx,
        fc_hz=config.fc_hz,
        fs_hz=config.fs_hz,
        cpi_s=config.cpi_s,
        config_fingerprint=config.fingerprint,
        address=address,
        clock_offset_s=round(offset_s, 3),
    )
