"""Identifier utilities shared across service modules."""

import hashlib
import re


def multinode_hex_from_key(key: str) -> str:
    """Return deterministic synthetic hex ID for a multinode solve key."""
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:10]
    return f"mn{digest}"


def passive_track_hex(track_id) -> str:
    """Synthetic hex ID for a tracker track with no ADS-B association.

    Single implementation for GeolocatedTrack.hex_id and the feed's pending
    detection_arcs so the same physical track keys identically everywhere in
    one process (hash() of a str is per-process salted — that's fine for a
    display key, but only if every producer derives it the same way).
    """
    return f"pr{abs(hash(track_id)) % 0xFFFF:04x}"


def normalize_hex_key(hex_code) -> str:
    """Canonical form for an ICAO hex used as a dict key: stripped, lowercased.

    Every path that writes ``state.adsb_aircraft`` (or cross-references it)
    must go through this — readers that dedupe lowercase first, so a raw
    uppercase key is a silent lookup miss, not an error.
    """
    return str(hex_code or "").strip().lower()


_TRANSPONDER_HEX_RE = re.compile(r"~?[0-9a-f]{6}")


def is_transponder_hex(hex_code) -> bool:
    """True if ``hex_code`` (already normalized) is a plausible transponder id.

    Six hex digits, with tar1090's ``~`` prefix allowed for non-ICAO TIS-B
    addresses.  Anything else — notably a simulator object id like
    ``obj-01373`` — must never enter the ADS-B world: a non-transponder id in
    ``state.adsb_aircraft`` (or in a solve's ``adsb_hex``) puts a dark target
    in the ADS-B lane, which starves the mn-dark-* track store and with it the
    entire dark-claiming path.
    """
    return bool(_TRANSPONDER_HEX_RE.fullmatch(str(hex_code or "")))
