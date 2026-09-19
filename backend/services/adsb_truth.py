"""Normalize independent ADS-B references without inventing missing measurements.

All returned records use tar1090 units plus explicit SI aliases. Capture time
belongs to the observation, never the HTTP poll. These references may identify
radar detections and score them; blind solver inputs must not contain them.
"""

import math

from config.constants import FT_TO_M, KNOTS_TO_MS
from services.geo import valid_latlon
from services.id_utils import is_transponder_hex, normalize_hex_key


def finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def reference_position_allowed(record: dict) -> bool:
    """Do not evaluate radar against another radar/MLAT-derived position."""
    if record.get("reference_eligible") is False:
        return False
    if record.get("type") in ("mlat", "tisb_icao", "tisb_other", "tisb_trackfile"):
        return False
    mlat, tisb = record.get("mlat") or [], record.get("tisb") or []
    return not mlat and not tisb or not any(k in mlat or k in tisb for k in ("lat", "lon"))


def normalize_reference(record: dict, hexn: str, *, source: str, world: str = "real") -> dict | None:
    """A usable position, with absent altitude/velocity kept absent."""
    if not reference_position_allowed(record):
        return None
    hexn = normalize_hex_key(hexn)
    lat, lon = record.get("lat"), record.get("lon")
    stamp = record.get("last_seen_ms")
    if not hexn or not is_transponder_hex(hexn) or not valid_latlon(lat, lon):
        return None
    if not all(finite(v) for v in (lat, lon, stamp)) or stamp <= 0:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    out = {**record, "hex": hexn, "source": source, "world": world, "timestamp_ms": stamp}
    alt = record.get("alt_m")
    altitude_source = record.get("altitude_source", "unspecified")
    if not finite(alt):
        feet = record.get("alt_baro")
        alt = feet * FT_TO_M if finite(feet) else None
        altitude_source = record.get("altitude_source", "barometric") if alt is not None else "missing"
    out["alt_m"] = alt
    out["altitude_source"] = altitude_source
    out["alt_baro"] = alt / FT_TO_M if alt is not None else None
    speed = record.get("velocity")
    if not finite(speed):
        knots = record.get("gs")
        speed = knots * KNOTS_TO_MS if finite(knots) else None
    heading = record.get("heading", record.get("track"))
    if not finite(heading):
        heading = None
    out["gs"] = speed / KNOTS_TO_MS if speed is not None else None
    out["track"] = heading
    out["velocity"] = speed
    out["heading"] = heading
    out["vel_east"] = speed * math.sin(math.radians(heading)) if speed is not None and heading is not None else None
    out["vel_north"] = speed * math.cos(math.radians(heading)) if speed is not None and heading is not None else None
    return out


def node_reference(record: dict, hexn: str, frame_ms: float, received_ms: float) -> dict | None:
    """Real node tags: keep their clock and missing fields, including legacy alt (ft)."""
    stamp = frame_ms
    time_basis = "node_frame_assumed"
    for key in ("timestamp_ms", "last_seen_ms", "timestamp"):
        if key in record:
            stamp = record[key]
            if key == "timestamp" and finite(stamp) and stamp < 100_000_000_000:
                stamp *= 1000
            time_basis = "node_position"
            break
    if not finite(stamp) or stamp <= 0 or stamp > received_ms + 2000:
        return None
    altitude = record.get("alt_baro")
    altitude_source = "barometric"
    if not finite(altitude):
        altitude = record.get("alt_geom")
        altitude_source = "geometric"
    if not finite(altitude):
        altitude = record.get("alt")
        altitude_source = record.get("altitude_source", "node_unspecified")
    fields = {
        k: record[k]
        for k in ("lat", "lon", "gs", "track", "flight", "type", "mlat", "tisb", "position_timestamp")
        if k in record
    }
    rec = normalize_reference(
        {
            **fields,
            "alt_baro": altitude,
            "last_seen_ms": stamp,
            "recv_ms": received_ms,
            "time_basis": time_basis,
            "altitude_source": altitude_source,
            "reference_eligible": record.get("reference_eligible", True),
            "precision_eligible": False,
        },
        hexn,
        source="node",
    )
    if rec is not None:
        rec["reference_eligible"] = all(finite(rec.get(k)) for k in ("alt_m", "vel_east", "vel_north"))
    return rec


def readsb_references(payload: dict, received_s: float, *, source: str = "adsb_service") -> dict[str, dict]:
    """Decode a readsb v2 envelope; MLAT/TIS-B positions are not ADS-B truth.

    Upstream feed latency can still be unknown (notably type=other). Preserve
    that fact; receipt-derived timestamps do not qualify as precision truth.
    """
    envelope_s = payload.get("now")
    # readsb's v2 re-api uses milliseconds; aircraft.json uses seconds.
    # Accept both envelopes without assigning a new age on receipt.
    if finite(envelope_s) and envelope_s > 100_000_000_000:
        envelope_s /= 1000
    if not finite(envelope_s) or envelope_s > received_s + 2 or envelope_s < received_s - 60:
        return {}
    rows = payload.get("ac", payload.get("aircraft", []))
    if not isinstance(rows, list):
        return {}
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not reference_position_allowed(row):
            continue
        age = row.get("seen_pos")
        if not finite(age) or age < 0 or age > 60:
            continue
        rec = normalize_reference(
            {
                **row,
                "last_seen_ms": int((envelope_s - age) * 1000),
                "recv_ms": int(received_s * 1000),
                "time_basis": "upstream_receipt",
                "precision_eligible": False,
            },
            row.get("hex"),
            source=source,
        )
        if rec is not None:
            prev = out.get(rec["hex"])
            if prev is None or rec["last_seen_ms"] > prev["last_seen_ms"]:
                out[rec["hex"]] = rec
    return out


def seeding_references(node_records: dict, service_records: dict, external_records: dict, world=None) -> dict:
    """Freshest complete fix per identity, with explicit real/simulation isolation.

    The default provider retains a simulation record on a hex collision for
    legacy consumers, whose own world gate then abstains for a real node.
    Frame-level callers request their world explicitly to resolve collisions.
    """
    out = {}
    prepared_fields = {
        "hex",
        "lat",
        "lon",
        "last_seen_ms",
        "timestamp_ms",
        "alt_baro",
        "gs",
        "track",
        "alt_m",
        "vel_east",
        "vel_north",
        "source",
        "world",
    }
    for records, source, default_world in (
        (external_records, "external", "real"),
        (service_records, "adsb_service", "real"),
        (node_records, "node", None),
    ):
        for hexn, raw in list(records.items()):
            rec_world = raw.get("world", default_world)
            if world is not None and rec_world is not None and rec_world != world:
                continue
            # The source pollers already normalize once at write time.
            # Reuse its complete records across frames, as the node cache
            # does, instead of repeating unit conversion and trig per frame.
            if prepared_fields.issubset(raw) and raw["hex"] == hexn and raw["timestamp_ms"] == raw["last_seen_ms"]:
                rec = (
                    raw
                    if reference_position_allowed(raw)
                    and all(finite(raw.get(k)) for k in ("lat", "lon", "timestamp_ms"))
                    and valid_latlon(raw["lat"], raw["lon"])
                    else None
                )
            else:
                rec = normalize_reference(raw, hexn, source=raw.get("source", source), world=rec_world)
            if rec is None or not all(finite(rec.get(k)) for k in ("alt_m", "vel_east", "vel_north")):
                continue
            prev = out.get(rec["hex"])
            if prev is None or (world is None and rec_world == "sim") or rec["last_seen_ms"] >= prev["last_seen_ms"]:
                out[rec["hex"]] = rec
    return out
