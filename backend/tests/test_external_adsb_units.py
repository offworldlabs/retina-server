"""`external_adsb_cache["velocity"]` is metres per second, whoever wrote it.

The two feeds disagreed: OpenSky's field is already m/s, adsb.lol's `gs` is
tar1090 knots and was stored raw, so every adsb.lol-sourced speed read ~1.94x
high in verification statistics, the solver's ADS-B-anchored seed and its
dead-reckoning.  adsb.lol is the only path that runs in production, OpenSky
returning 429 for anonymous access, so that was every external entry rather
than a fallback minority.  ClickUp 86cb9br6k.
"""

import asyncio

import pytest

from clients.adsb_lol import AdsbLolClient
from config.constants import KNOTS_TO_MS as _KNOTS_TO_MS
from services.tasks.periodic import _fetch_adsb_lol, _opensky_entry

# One speed, expressed the way each feed expresses it.
_CRUISE_KNOTS = 450.0
_CRUISE_MS = _CRUISE_KNOTS * _KNOTS_TO_MS


def _stub_adsb_lol(monkeypatch, aircraft):
    import services.tasks.periodic as periodic

    monkeypatch.setattr(periodic, "_adsb_lol_client", None, raising=False)
    monkeypatch.setattr(AdsbLolClient, "fetch_all", lambda self: aircraft)


def _opensky_row(velocity_ms):
    """3 time_position, 5 lon, 6 lat, 7 alt_m, 9 velocity (m/s), 10 track."""
    return ["abc123", "CALL    ", "UK", 1_700_000_000.0, 1_700_000_000.0, 16.0, 48.0, 10000.0, False, velocity_ms, 90.0]


def _lol_row(gs_knots):
    return {"hex": "abc123", "lat": 48.0, "lon": 16.0, "alt_baro": 32808, "gs": gs_knots, "track": 90.0}


def test_adsb_lol_ground_speed_is_converted_from_knots(monkeypatch):
    _stub_adsb_lol(monkeypatch, [_lol_row(_CRUISE_KNOTS)])

    cache = asyncio.run(_fetch_adsb_lol(48.0, 16.0))

    assert cache["abc123"]["velocity"] == pytest.approx(_CRUISE_MS)


def test_opensky_velocity_is_stored_unconverted():
    """Its field is already m/s — the guard against fixing this in the wrong place."""
    entry = _opensky_entry(_opensky_row(_CRUISE_MS), 1_700_000_000.0)

    assert entry["velocity"] == pytest.approx(_CRUISE_MS)


def test_both_feeds_agree_on_one_aircrafts_speed(monkeypatch):
    """The acceptance criterion: same aircraft, either source, same number."""
    _stub_adsb_lol(monkeypatch, [_lol_row(_CRUISE_KNOTS)])

    from_lol = asyncio.run(_fetch_adsb_lol(48.0, 16.0))["abc123"]["velocity"]
    from_opensky = _opensky_entry(_opensky_row(_CRUISE_MS), 1_700_000_000.0)["velocity"]

    assert from_lol == pytest.approx(from_opensky)


def test_a_non_numeric_ground_speed_becomes_unknown_rather_than_raising(monkeypatch):
    """The field was passed through raw before, so it was never required to be a number."""
    _stub_adsb_lol(monkeypatch, [_lol_row("n/a")])

    assert asyncio.run(_fetch_adsb_lol(48.0, 16.0))["abc123"]["velocity"] is None
