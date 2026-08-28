"""The one configuration validator, shared by registration and PUT /nodes/config,
and the one normaliser every in-process copy of a node's config passes through.

Bounds are the wire contract's, at version 1.1.1. Three checks are here and not
there because a JSON schema cannot express them: a receiver and illuminator at
the same point give the solver a degenerate baseline; bool is a subclass of int
in Python, so a plain range check accepts True as a latitude of 1; and NaN
compares false against every bound, so it survives a range check untouched.

A leaf on purpose. It takes a dict and returns a dict, knowing nothing of identity,
HTTP or status codes, so both callers can share it and it stays testable without a
database. Nothing beyond the standard library may be imported here.
"""

import math
from typing import Any, Literal

# About 0.11 m. Below this the receiver and illuminator are the same point as far as
# the solver is concerned, whatever the node believes it measured.
_MIN_BASELINE_DEG = 1e-6


class ConfigInvalid(Exception):
    def __init__(self, field: str, reason: str = "out of range") -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


# field -> (low, high, low_inclusive, high_inclusive). beam_width_deg and
# beam_azimuth_deg are absent because both are nullable and handled separately.
_NUMERIC_BOUNDS: dict[str, tuple[float, float, bool, bool]] = {
    "rx_lat": (-90, 90, True, True),
    "rx_lon": (-180, 180, True, True),
    "rx_alt_ft": (-1500, 30000, True, True),
    "tx_lat": (-90, 90, True, True),
    "tx_lon": (-180, 180, True, True),
    "tx_alt_ft": (-1500, 30000, True, True),
    "fc_hz": (1_000_000, 6_000_000_000, True, True),
    "fs_hz": (100_000, 20_000_000, True, True),
    "max_range_km": (0, 1000, False, True),
    "cpi_s": (0, 10, False, True),
    # The contract sets no maximum on either tolerance, so the bound is math.inf
    # rather than a ceiling of our own: _number has already rejected infinities, so
    # nothing that reaches here can equal it, and inventing a limit would reject a
    # config the contract permits. A plausibility bound is a separate decision from
    # a conformance one, and this module only makes the latter.
    "delay_tolerance_us": (0, math.inf, False, True),
    "doppler_tolerance_hz": (0, math.inf, False, True),
}

# Nullable since 1.1.3. An owner setting a node up cannot always supply the
# geometry, and a substituted coordinate would be wrong data the server could
# not later tell apart from a survey. Latitude and longitude are a pair;
# altitude stands alone, because it is a small term that already defaults to
# zero wherever the geodesy reads it.
_NULLABLE = {"rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft"}

_REQUIRED = set(_NUMERIC_BOUNDS) | {"tx_callsign", "beam_width_deg", "beam_azimuth_deg"}


def _number(field: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigInvalid(field, "not a number")
    try:
        number = float(value)
    except OverflowError:
        # JSON puts no ceiling on integer literals, so a node can send an int with no
        # float representation. Rejected rather than left to raise out of the route.
        raise ConfigInvalid(field, "out of range") from None
    if not math.isfinite(number):
        raise ConfigInvalid(field, "not a finite number")
    return number


def validate_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the normalised configuration, or raise ConfigInvalid naming one field."""
    # A JSON body is not necessarily an object, and every check below assumes it is:
    # set(None) and sorted() over mixed types both raise TypeError, which would leave
    # the route with no ConfigInvalid to map and answer a malformed body with a 500.
    # "config" is the key registration nests this under, and the whole body on PUT.
    if not isinstance(payload, dict):
        raise ConfigInvalid("config", "not an object")

    # Sorted so that a payload wrong in several places always names the same field,
    # and a node retrying unchanged always gets the same answer.
    unknown = sorted(set(payload) - _REQUIRED)
    if unknown:
        raise ConfigInvalid(unknown[0], "unknown field")

    missing = sorted(_REQUIRED - set(payload))
    if missing:
        raise ConfigInvalid(missing[0], "missing")

    out: dict[str, Any] = {}
    for field, (low, high, low_inclusive, high_inclusive) in _NUMERIC_BOUNDS.items():
        if payload[field] is None and field in _NULLABLE:
            out[field] = None
            continue
        value = _number(field, payload[field])
        below = value < low if low_inclusive else value <= low
        above = value > high if high_inclusive else value >= high
        if below or above:
            raise ConfigInvalid(field)
        out[field] = value

    callsign = payload["tx_callsign"]
    if not isinstance(callsign, str) or not 1 <= len(callsign) <= 32:
        raise ConfigInvalid("tx_callsign")
    out["tx_callsign"] = callsign

    # Required and nullable since 1.1.1: no node has its antenna characterised,
    # because retina-gui does not collect the geometry from owners, so null is what
    # the whole fleet sends. Substituting a width would be wrong data the server
    # could not later tell apart from a measurement, and rejecting it left no node
    # able to register at all. The interval is (0, 360], not the azimuth's below.
    width = payload["beam_width_deg"]
    if width is None:
        out["beam_width_deg"] = None
    else:
        width = _number("beam_width_deg", width)
        if not 0 < width <= 360:
            raise ConfigInvalid("beam_width_deg")
        out["beam_width_deg"] = width

    # null is meaningful: the node is broadside or omnidirectional. Coercing it to 0.0
    # would silently aim every unaimed node in the fleet due north.
    azimuth = payload["beam_azimuth_deg"]
    if azimuth is None:
        out["beam_azimuth_deg"] = None
    else:
        azimuth = _number("beam_azimuth_deg", azimuth)
        if not 0 <= azimuth < 360:
            raise ConfigInvalid("beam_azimuth_deg")
        out["beam_azimuth_deg"] = azimuth

    # A latitude without its longitude places nothing, so a half-supplied side
    # is a bug upstream rather than a state worth representing.
    for lat_field, lon_field in (("rx_lat", "rx_lon"), ("tx_lat", "tx_lon")):
        if (out[lat_field] is None) != (out[lon_field] is None):
            unpaired = lat_field if out[lat_field] is None else lon_field
            raise ConfigInvalid(unpaired, "latitude and longitude must be given together")

    if (
        out["rx_lat"] is not None
        and out["tx_lat"] is not None
        and (
            abs(out["rx_lat"] - out["tx_lat"]) < _MIN_BASELINE_DEG
            and abs(out["rx_lon"] - out["tx_lon"]) < _MIN_BASELINE_DEG
        )
    ):
        raise ConfigInvalid("tx_lat", "receiver and illuminator are at the same point")

    return out


_COORDINATE_PAIRS = (("rx_lat", "rx_lon"), ("tx_lat", "tx_lon"))

# The flat spelling nodes predating rx_/tx_ still send. services.tcp_handler
# accepts it, so a node using it is placed and must read as placed.
_LEGACY_COORDINATES = (("rx_lat", "lat"), ("rx_lon", "lon"))

# Terrain figures, not measurements. pipeline.passive_radar and
# retina_geolocator.multinode_solver each multiply an altitude by a metre
# conversion the moment they are handed one, so neither may ever see a null;
# retina_analytics.association takes one too, spelled `or 0`, which survives a
# null by silently reading it as sea level.
#
# Applied at the geometry boundary, never on the way in: a config that reaches
# publication or the Parquet archive must carry the altitude the node declared,
# nulls included, because nothing downstream could later tell a working figure
# apart from a survey and archive rows are not correctable once published.
# resolve_altitudes below is the only caller.
ALTITUDE_DEFAULT_FT = {"rx_alt_ft": 900.0, "tx_alt_ft": 1200.0}


def resolve_altitudes(cfg: dict) -> dict:
    """``cfg`` with a null altitude replaced by its terrain default.

    The geometry boundary, and the counterpart to canonical_config: that keeps
    a declared null null, because publication and the archive must not carry an
    invented figure, and this resolves it for the geodesy, which cannot take
    one. Apply it at every door into geometry and nowhere earlier, so that one
    missing altitude cannot become 900 ft in one subsystem and 0 ft in another.

    Keyed on None, not falsiness: a receiver at 0 ft is at sea level, not
    unsurveyed, and ``or`` would silently lift it to 900.

    Copies, so resolving cannot write the working figure back into the dict
    that publication and the archive read.
    """
    resolved = dict(cfg)
    for field, default in ALTITUDE_DEFAULT_FT.items():
        if resolved.get(field) is None:
            resolved[field] = default
    return resolved


def _finite_float(value: Any) -> float | None:
    """The value as a float, or None when it cannot be one.

    The non-raising sibling of _number, for config dicts that never passed
    through validate_config and may hold anything JSON can express.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        # JSON puts no ceiling on integer literals, so a node can send an int
        # with no float representation.
        return None
    return number if math.isfinite(number) else None


def canonical_config(raw: Any) -> dict[str, Any]:
    """The one in-memory shape of a node's config, for every consumer to read.

    Returns a new dict, leaving ``raw`` untouched, in which:

    - ``rx_lat``, ``rx_lon``, ``tx_lat`` and ``tx_lon`` are each a float or
      None, always present. None means the end is not placed, covering absent,
      null, unusable, half a pair, and the exact (0, 0) sentinel that was the
      only representable "unknown" while the columns were NOT NULL. A single
      zero axis is a real coordinate and survives.
    - ``rx_alt_ft`` and ``tx_alt_ft`` are each a float or None, always present.
      None is the honest answer and is left standing here; geometry resolves it
      through ``frame_processor.resolve_altitudes`` at the two boundaries that
      cannot take a null.
    - the legacy flat ``lat``/``lon`` fold into ``rx_lat``/``rx_lon`` and are
      gone from the result.
    - every other key passes through unchanged.

    Never raises, for any input, including a non-dict.

    Corrects, never invents. Every transformation above turns an unusable value
    into the null it already meant, or a coordinate into the number it already
    was, so the result is safe to publish and to archive as well as to solve on
    — which is why there is one shape here and not a canonical/declared pair.

    Called wherever a config enters shared in-process state, so downstream code
    may read a coordinate as a number or a null and nothing else. The durable
    ``node_configs`` row is not canonicalised: it keeps its honest nulls.
    """
    if not isinstance(raw, dict):
        return {}
    config = dict(raw)

    # Keyed on absence, not falsiness: rx_lat present and explicitly null is a
    # positionless registration, which a stray legacy lat must not overrule.
    for field, legacy in _LEGACY_COORDINATES:
        if field not in config and legacy in config:
            config[field] = config[legacy]
    config.pop("lat", None)
    config.pop("lon", None)

    for lat_field, lon_field in _COORDINATE_PAIRS:
        lat = _finite_float(config.get(lat_field))
        lon = _finite_float(config.get(lon_field))
        if lat is None or lon is None or (lat == 0.0 and lon == 0.0):
            lat = lon = None
        config[lat_field] = lat
        config[lon_field] = lon

    for field in ALTITUDE_DEFAULT_FT:
        config[field] = _finite_float(config.get(field))

    return config


PositionStatus = Literal["positioned", "missing_rx", "missing_tx", "missing_both"]


def position_status(config: dict[str, Any]) -> PositionStatus:
    """Which ends of the bistatic pair this config places.

    One value for consumers to branch on, rather than four fields each of them
    has to recombine. Keyed on latitude and longitude alone: a node with a
    position and no altitude is positioned.

    Reads a canonical_config, where an unplaced end is None on both axes.
    """
    has_rx = config.get("rx_lat") is not None and config.get("rx_lon") is not None
    has_tx = config.get("tx_lat") is not None and config.get("tx_lon") is not None
    if has_rx and has_tx:
        return "positioned"
    if has_rx:
        return "missing_tx"
    if has_tx:
        return "missing_rx"
    return "missing_both"
