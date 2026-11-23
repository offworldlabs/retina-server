"""The one configuration validator, shared by registration and PUT /nodes/config,
and the one normaliser every in-process copy of a node's config passes through.

These bounds are the wire contract's, and they are its source: config_json_schema
below is what the document publishes, so the contract is built from this module
rather than copied into it. Three checks cannot travel with them, because a JSON
schema expresses none of them: a receiver and illuminator at the same point give
the solver a degenerate baseline; bool is a subclass of int in Python, so a plain
range check accepts True as a latitude of 1; and NaN compares false against every
bound, so it survives a range check untouched.

A leaf on purpose. It takes a dict and returns a dict, knowing nothing of identity,
HTTP or status codes, so both callers can share it and it stays testable without a
database. Nothing beyond the standard library may be imported here.
"""

import math
from collections.abc import Callable
from copy import deepcopy
from typing import Any, Literal

# About 0.11 m. Below this the receiver and illuminator are the same point as far as
# the solver is concerned, whatever the node believes it measured.
_MIN_BASELINE_DEG = 1e-6


class ConfigInvalid(Exception):
    def __init__(self, field: str, reason: str = "out of range") -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


# field -> (low, high, low_inclusive, high_inclusive), each published by
# config_json_schema and enforced by _bounded from this one literal.
# beam_width_deg and beam_azimuth_deg are absent because both are nullable and
# handled separately.
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
    # The hardware's, optional since 1.7.0 and so checked where _OPTIONAL_CHECKS
    # puts them rather than in validate_config's loop over the rest. A pair's
    # bound is each element's, and lna_state's is a whole number's.
    "gain_reduction_db": (0, 100, True, True),
    "lna_state": (0, 255, True, True),
    "antenna_gain_dbi": (-20, 40, True, True),
    "feedline_loss_db": (0, 30, True, True),
}

# Nullable since 1.1.3. An owner setting a node up cannot always supply the
# geometry, and a substituted coordinate would be wrong data the server could
# not later tell apart from a survey. Latitude and longitude are a pair;
# altitude stands alone, because it is a small term that already defaults to
# zero wherever the geodesy reads it.
_NULLABLE = {"rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft"}

# The three fields the table above does not carry: the callsign, which is not
# numeric, and the two beam fields, whose bounds are checked beside it because
# both are nullable. Written out rather than derived, so
# tests/test_node_config_validation.py exercises every bound published here
# against what validate_config actually accepts: that is what stops these
# drifting from the checks below, since they are not the same literals.
_UNTABLED_PROPERTIES: dict[str, dict[str, Any]] = {
    "tx_callsign": {"type": "string", "minLength": 1, "maxLength": 32},
    "beam_width_deg": {"type": "number", "exclusiveMinimum": 0.0, "maximum": 360.0},
    "beam_azimuth_deg": {"type": "number", "minimum": 0.0, "exclusiveMaximum": 360.0},
}

# Nullable beyond the coordinates, and for a different reason: no node has its
# antenna characterised, so null is what the whole fleet sends for both.
_NULLABLE_BEAM = {"beam_width_deg", "beam_azimuth_deg"}

# Nullable since 1.2.2, on the coordinates' reasoning above: an owner who cannot
# name the illuminator has nothing to put here, and a substituted name is wrong
# data the server could not later tell apart from a real one. The empty string
# stays refused, so null is the single spelling of "unknown" and no stored row
# can hold the other.
_NULLABLE_CALLSIGN = {"tx_callsign"}

_SCHEMA_DESCRIPTION = """\
The receiver and illuminator geometry, the radio parameters, the association
tolerances and the node's hardware. The fifteen fields under `required` must be
sent.

The nine hardware descriptors, from `sdr_model` to `notch_filters`, are optional
and each is nullable: absent and null both mean unknown. A descriptor that is
sent is checked like any other field, and one out of bounds refuses the whole
configuration.

The six coordinate fields are nullable, for a node whose owner cannot supply the
geometry. Such a node registers and streams, and its detections are counted, but
it places nothing on the map until a position arrives. A latitude and its
longitude are given together or both null.

`tx_callsign` is nullable for the same reason, and the empty string is not: a
node that cannot name its illuminator sends null, which is the only way to say
so. `sdr_model` and `antenna_model` refuse the empty string on the same terms.

Necessary but not sufficient. A receiver and illuminator at the same point are
refused, as is a value that is not a finite number, and neither is expressible
here: both answer `400 invalid_config` naming the field."""


def _numeric_property(
    low: float,
    high: float,
    low_inclusive: bool,
    high_inclusive: bool,
    json_type: Literal["number", "integer"] = "number",
) -> dict[str, Any]:
    # An unbounded end is what JSON Schema says by omission: neither an infinity
    # nor a NaN is a bound, and either would publish a ceiling the server does
    # not have in a literal no JSON parser can read. Both ends are guarded, so
    # adding an unbounded low to the table cannot slip one out.
    as_bound = int if json_type == "integer" else float
    schema: dict[str, Any] = {"type": json_type}
    if math.isfinite(low):
        schema["minimum" if low_inclusive else "exclusiveMinimum"] = as_bound(low)
    if math.isfinite(high):
        schema["maximum" if high_inclusive else "exclusiveMaximum"] = as_bound(high)
    return schema


# The node's hardware, optional and nullable since 1.7.0. Absent and null both
# mean unknown: a node predating them sends none, an owner who does not know the
# antenna has nothing to put here, and a substituted value would be wrong data
# the server could not later tell apart from a report.
#
# Conformance bounds, not plausibility ones, and wide on purpose: a value out of
# them refuses the node's whole registration, so a bound tighter than what the
# hardware can report would lock a working node out over a descriptor.
#
# The four magnitudes are published from _NUMERIC_BOUNDS, which their checks
# read too. The two lengths are written out for the reason _UNTABLED_PROPERTIES
# is, and checked the same way: _OPTIONAL_CHECKS below reads its own literals,
# and the tests walk every bound published here against validate_config. The
# polarisations are one tuple both read, since no bound-walk can reach an enum's
# members.
_POLARISATIONS = ("horizontal", "vertical", "circular")

_OPTIONAL_PROPERTIES: dict[str, dict[str, Any]] = {
    "sdr_model": {"type": "string", "minLength": 1, "maxLength": 64},
    "gain_reduction_db": {
        "description": "The SDR's gain reduction in dB, as `[reference, surveillance]`.",
        "type": "array",
        "items": _numeric_property(*_NUMERIC_BOUNDS["gain_reduction_db"]),
        "minItems": 2,
        "maxItems": 2,
    },
    "lna_state": {
        "description": "The SDR's LNA state register, whose meaning depends on `sdr_model`.",
        **_numeric_property(*_NUMERIC_BOUNDS["lna_state"], json_type="integer"),
    },
    "antenna_model": {"type": "string", "minLength": 1, "maxLength": 64},
    "antenna_gain_dbi": _numeric_property(*_NUMERIC_BOUNDS["antenna_gain_dbi"]),
    "antenna_polarisation": {"type": "string", "enum": list(_POLARISATIONS)},
    "feedline_loss_db": _numeric_property(*_NUMERIC_BOUNDS["feedline_loss_db"]),
    "local_adsb_receiver": {"type": "boolean"},
    "notch_filters": {
        "description": "Whether each notch filter is on, as `[dab, rf]`.",
        "type": "array",
        "items": {"type": "boolean"},
        "minItems": 2,
        "maxItems": 2,
    },
}

# Derived from the tables rather than listed again, so a field added to one is
# known to the validator and published as required or optional in the same
# edit. A field in _NUMERIC_BOUNDS is required unless it is also here.
_OPTIONAL = set(_OPTIONAL_PROPERTIES)
_REQUIRED = (set(_NUMERIC_BOUNDS) | set(_UNTABLED_PROPERTIES)) - _OPTIONAL


def _or_null(schema: dict[str, Any]) -> dict[str, Any]:
    # A description is the field's rather than its non-null branch's, so it stays
    # beside the anyOf, which is where a generated client reads it from.
    branch = {key: value for key, value in schema.items() if key != "description"}
    described = {"description": schema["description"]} if "description" in schema else {}
    return described | {"anyOf": [branch, {"type": "null"}]}


def config_json_schema() -> dict[str, Any]:
    """The configuration's published JSON Schema, built from the tables above.

    Generated from what this module enforces rather than written beside it, so
    the document a node is built against cannot state a bound the server does
    not apply. Describing the shape is not enforcing it; registration's body
    stays untyped, for the reason routes/node_register.py gives.

    Fresh every call, nested dicts included. Two callers publish this and the
    frameworks under both mutate schema dicts in place, so a shared sub-dict
    would let one operation's published bounds rewrite the other's.
    """
    properties = {field: _numeric_property(*bounds) for field, bounds in _NUMERIC_BOUNDS.items() if field in _REQUIRED}
    properties |= deepcopy(_UNTABLED_PROPERTIES)
    properties |= deepcopy(_OPTIONAL_PROPERTIES)
    nullable = _NULLABLE | _NULLABLE_BEAM | _NULLABLE_CALLSIGN | _OPTIONAL
    return {
        "type": "object",
        "title": "NodeConfig",
        "description": _SCHEMA_DESCRIPTION,
        "properties": {
            field: _or_null(schema) if field in nullable else schema for field, schema in properties.items()
        },
        # Sorted for the same reason the refusals above are: a document that
        # reordered between runs would show as a diff in the CI gate. The
        # hardware descriptors are absent here, as they may be from a payload.
        "required": sorted(_REQUIRED),
        # The validator names an unknown key back to the caller rather than
        # ignoring it.
        "additionalProperties": False,
    }


def _typed_branch(published: dict[str, Any], json_type: str) -> dict[str, Any]:
    for alternative in published.get("anyOf", [published]):
        if alternative.get("type") == json_type:
            return alternative
    return published


def numeric_branch(published: dict[str, Any]) -> dict[str, Any]:
    """Where a published property's magnitude bounds sit, whether or not it is
    nullable: its number or integer half, or a pair's items.

    Beside the builder because it is how the builder's output is read back, and
    the alternative is each caller re-deriving where the bounds live. Selects on
    the type rather than on position in the `anyOf`, so reordering the branches
    cannot leave a caller reading the null one and finding no bounds at all.
    """
    for alternative in published.get("anyOf", [published]):
        bounded = alternative["items"] if alternative.get("type") == "array" else alternative
        if bounded.get("type") in ("number", "integer"):
            return bounded
    return published


def string_branch(published: dict[str, Any]) -> dict[str, Any]:
    """numeric_branch for the fields whose bounds are lengths, not magnitudes."""
    return _typed_branch(published, "string")


def _as_finite_float(value: Any) -> tuple[float | None, str]:
    """The value as a finite float, with the reason when it cannot be one.

    Shared by the raising and non-raising doors below so the two cannot drift
    on what counts as a real number, which is the property this whole module
    turns on.  The reason is what _number reports and _finite_float discards.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "not a number"
    try:
        number = float(value)
    except OverflowError:
        # JSON puts no ceiling on integer literals, so a node can send an int with no
        # float representation. Rejected rather than left to raise out of the route.
        return None, "out of range"
    if not math.isfinite(number):
        return None, "not a finite number"
    return number, ""


def _number(field: str, value: Any) -> float:
    number, reason = _as_finite_float(value)
    if number is None:
        raise ConfigInvalid(field, reason)
    return number


def _bounded(field: str, value: Any) -> float:
    """The value as a number within the field's _NUMERIC_BOUNDS, or ConfigInvalid naming it."""
    low, high, low_inclusive, high_inclusive = _NUMERIC_BOUNDS[field]
    number = _number(field, value)
    below = number < low if low_inclusive else number <= low
    above = number > high if high_inclusive else number >= high
    if below or above:
        raise ConfigInvalid(field)
    return number


def _length_within(low: int, high: int) -> Callable[[str, Any], str]:
    # For the callsign and the two model names, whose null is the one spelling
    # of unknown, so "" is refused rather than read as a shorter way to say it.
    def check(field: str, value: Any) -> str:
        if isinstance(value, str) and low <= len(value) <= high:
            return value
        raise ConfigInvalid(field)

    return check


_callsign = _length_within(1, 32)

# The checks for the optional fields, each taking a value already known not to
# be null. Every refusal names the field itself, a pair's included, since a node
# corrects the field and not one of its elements.
_model_name = _length_within(1, 64)


def _register_state(field: str, value: Any) -> int:
    # The contract is JSON Schema 2020-12, which counts 3.0 as an integer, so a
    # whole float is one here too and is stored as the int it is. _number has
    # already refused a bool.
    number = _number(field, value)
    if not number.is_integer():
        raise ConfigInvalid(field, "not an integer")
    return int(_bounded(field, number))


def _polarisation(field: str, value: Any) -> str:
    # Membership in a tuple compares rather than hashes, so a list sent here is
    # refused instead of raising TypeError.
    if isinstance(value, str) and value in _POLARISATIONS:
        return value
    raise ConfigInvalid(field, "not a polarisation")


def _flag(field: str, value: Any) -> bool:
    # True or False only. A bool is an int in Python, but 1 is not a bool, and a
    # node sending it has the type wrong.
    if isinstance(value, bool):
        return value
    raise ConfigInvalid(field, "not a boolean")


def _pair(element: Callable[[str, Any], Any]) -> Callable[[str, Any], list[Any]]:
    # A list as JSON decodes one, or a tuple from an in-process caller, but never
    # a string, which is also a sequence and could be two characters long.
    def check(field: str, value: Any) -> list[Any]:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ConfigInvalid(field, "not a pair")
        return [element(field, item) for item in value]

    return check


# In the published order, which is the order they are checked in.
_OPTIONAL_CHECKS: dict[str, Callable[[str, Any], Any]] = {
    "sdr_model": _model_name,
    "gain_reduction_db": _pair(_bounded),
    "lna_state": _register_state,
    "antenna_model": _model_name,
    "antenna_gain_dbi": _bounded,
    "antenna_polarisation": _polarisation,
    "feedline_loss_db": _bounded,
    "local_adsb_receiver": _flag,
    "notch_filters": _pair(_flag),
}


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
    unknown = sorted(set(payload) - _REQUIRED - _OPTIONAL)
    if unknown:
        raise ConfigInvalid(unknown[0], "unknown field")

    missing = sorted(_REQUIRED - set(payload))
    if missing:
        raise ConfigInvalid(missing[0], "missing")

    out: dict[str, Any] = {}
    for field in _NUMERIC_BOUNDS:
        if field in _OPTIONAL:
            continue  # with the rest of the hardware, below
        if payload[field] is None and field in _NULLABLE:
            out[field] = None
            continue
        out[field] = _bounded(field, payload[field])

    # Null is the illuminator being unnamed, and "" is not a shorter way to say
    # it: see _NULLABLE_CALLSIGN.
    callsign = payload["tx_callsign"]
    out["tx_callsign"] = None if callsign is None else _callsign("tx_callsign", callsign)

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

    # Absent and null are one answer for these, unknown, and both come out as
    # None, so the output carries every field whatever the node sent. Checked
    # before the rules relating fields, as the bounds above are.
    for field, check in _OPTIONAL_CHECKS.items():
        value = payload.get(field)
        out[field] = None if value is None else check(field, value)

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
    through validate_config and may hold anything JSON can express.  Unlike
    _number it parses a numeric string first, which such a dict does carry.
    """
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return _as_finite_float(value)[0]


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
      through ``resolve_altitudes`` at the three doors that cannot take a null.
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


# fc_hz is the v1 wire's and a polled radar's, FC the TCP fleet's, and
# frequency older records'.
_CARRIER_KEYS = ("fc_hz", "FC", "frequency")


def carrier_hz(config: dict[str, Any]) -> Any:
    """A node's carrier frequency in Hz, under whichever key its config spells it, or None."""
    for key in _CARRIER_KEYS:
        if config.get(key) is not None:
            return config[key]
    return None
