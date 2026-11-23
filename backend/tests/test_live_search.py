"""Live search over the real-only feed, built from what the flush publishes.

Every frame goes through aircraft_flush._flush_once, so the private-node
redaction, the real-only filter and the ref substitution all run before the
index sees the bytes, as they do live.
"""

import asyncio

import pytest

from core import state
from core.nodes import Node
from core.users import async_session_maker
from services.live_search import LiveFilters, live_index
from services.tasks.aircraft_flush import _flush_once
from services.tracks_store import BBox

_A, _B, _PRIV, _SYN, _SYN2 = "retalpha01", "retbravo02", "retpriv03", "synth-n1", "synth-n2"
_REF_A, _REF_B, _REF_PRIV = "ndealpha0000001", "ndebravo0000002", "ndepriv00000003"
_TRAIL = [[51.98, 0.48, 11800.0, 1762041598.0], [51.99, 0.49, None, 1762041599.0], [52.0, 0.5, 12000.0, 1762041600.0]]


def _ac(hex_code: str, kind: str, source: str, lat, lon, alt, gs, **extra) -> dict:
    """One entry as the feed builders write it, with fields no row may carry."""
    entry = {"hex": hex_code, "type": kind, "position_source": source, "lat": lat, "lon": lon, "alt_baro": alt}
    return entry | {"gs": gs, "track": 90.0, "seen": 1.0, "messages": 7, "rssi": -10.0, "alt_geom": alt} | extra


def _mn(hex_code: str, lat, lon, alt, gs, *ids: str, **extra) -> dict:
    mn = {"multinode": True, "n_nodes": len(ids), "contributing_node_ids": list(ids), "flight": f"MN{len(ids)}N"}
    return _ac(hex_code, "multinode_solve", "multinode_solve", lat, lon, alt, gs, **mn, **extra)


def _frame() -> dict:
    track = {"flight": "ba1234", "node_id": _B, "recent_positions": _TRAIL, "is_anomalous": False, "track": 45.0}
    both = {"n_nodes": 2, "contributing_node_ids": [_A, _B]}
    aircraft = [
        _ac("4ca1b2", "adsb_icao", "adsb_single_node", 51.47, -0.45, 4000, 180.0, flight="BAW12", node_id=_A),
        _ac("ba1234", "tisb_other", "solver_adsb_seed", 52.0, 0.5, 12000, 250.0, **track, seen=1.5),
        _mn("mn1f2e3d4c5b", 60.0, 10.3325, 30000, 420.0, _A, _B, pos_sigma_m=85.0, is_anomalous=True),
        _mn("mnaaaa000001", 60.0, 10.3335, 8000, 300.0, _PRIV, _A),
        _ac("pr0a1b", "tisb_other", "single_node_ellipse_arc", -16.5, 178.5, None, 120.0, flight="", node_id=_A),
        _ac("c0ffee", "adsb_icao", "adsb_single_node", -17.0, 179.6, 38000, 480.0, flight="FJI910", node_id=_B),
        _ac("c0ffef", "adsb_icao", "adsb_multi_node", -17.2, -179.7, 36000, 455.0, flight="QFA3", **both),
        _ac("pr9f9f", "tisb_other", "solver_single_node", None, None, 9000, None, flight="", node_id=_B),
        _ac("5a5a5a", "tisb_other", "solver_single_node", 51.5, 0.0, 9000, 200.0, flight="", node_id=_SYN),
        _mn("mn0123456789", 51.6, 0.1, 9000, 200.0, _SYN, _SYN2),
        _ac("dead01", "adsb_icao", "adsb_single_node", 51.7, 0.2, 9000, 200.0, flight="BAW99", node_id=_PRIV),
    ]
    return {"now": 1762041600.0, "messages": len(aircraft), "aircraft": aircraft, "detection_arcs": []}


@pytest.fixture()
def nodes():
    async def _seed():
        async with async_session_maker() as session:
            session.add_all([Node(node_id=_A, node_ref=_REF_A), Node(node_id=_B, node_ref=_REF_B)])
            session.add(Node(node_id=_PRIV, node_ref=_REF_PRIV, publication="private"))
            await session.commit()

    asyncio.run(_seed())
    for nid in (_A, _B, _PRIV, _SYN, _SYN2):
        state.connected_nodes[nid] = {"status": "active", "is_synthetic": nid.startswith("synth-")}


@pytest.fixture()
def index(nodes):
    _flush_once(_frame())
    return live_index()


def _hexes(rows: list[dict]) -> set[str]:
    return {row["hex"] for row in rows}


def test_bbox_keeps_its_aircraft_including_across_the_antimeridian(index):
    assert _hexes(index.search(LiveFilters(bbox=BBox(51.0, -1.0, 52.5, 1.0)))) == {"4ca1b2", "ba1234"}
    assert _hexes(index.search(LiveFilters(bbox=BBox(-20.0, 179.0, -15.0, -179.0)))) == {"c0ffee", "c0ffef"}
    world = {"4ca1b2", "ba1234", "mn1f2e3d4c5b", "mnaaaa000001", "pr0a1b", "c0ffee", "c0ffef"}
    assert _hexes(index.search(LiveFilters(bbox=BBox(-90.0, -180.0, 90.0, 180.0)))) == world


def test_radius_is_great_circle_nautical_miles(index):
    # 34 m inside and 22 m outside 10 nm, due east at 60 N.
    assert _hexes(index.search(LiveFilters(center=(60.0, 10.0), radius_nm=10.0))) == {"mn1f2e3d4c5b"}


def test_altitude_range_is_inclusive_and_skips_unknown_altitude(index):
    hexes = {"4ca1b2", "ba1234", "mnaaaa000001", "pr9f9f"}
    assert _hexes(index.search(LiveFilters(alt_min_ft=4000, alt_max_ft=12000))) == hexes


def test_ground_speed_range_is_inclusive(index):
    assert _hexes(index.search(LiveFilters(gs_min_kt=180, gs_max_kt=300))) == {"4ca1b2", "ba1234", "mnaaaa000001"}


def test_hex_list_matches_any_case_and_ands_with_other_filters(index):
    hexes = frozenset({"BA1234", "Mn1F2E3D4C5B", "ffffff"})
    assert _hexes(index.search(LiveFilters(hexes=hexes))) == {"ba1234", "mn1f2e3d4c5b"}
    assert _hexes(index.search(LiveFilters(hexes=hexes, alt_min_ft=20000))) == {"mn1f2e3d4c5b"}


def test_callsign_prefix_matches_real_adsb_callsigns_only(index):
    assert _hexes(index.search(LiveFilters(callsign_prefix=" ba "))) == {"4ca1b2"}
    assert _hexes(index.search(LiveFilters(callsign_prefix="MN"))) == set()
    rows = {row["hex"]: row for row in index.search(LiveFilters())}
    assert "flight" not in rows["ba1234"] and "flight" not in rows["mn1f2e3d4c5b"]


def test_private_and_synthetic_entries_are_never_indexed(index):
    rows = index.search(LiveFilters())
    full = {"4ca1b2", "ba1234", "mn1f2e3d4c5b", "mnaaaa000001", "pr0a1b", "c0ffee", "c0ffef", "pr9f9f"}
    assert _hexes(rows) == full
    assert {ref for row in rows for ref in row["node_refs"]} == {_REF_A, _REF_B}


def test_lookup_returns_the_row_and_its_live_trail(index):
    assert index.lookup(hex="BA1234") == [
        {
            "hex": "ba1234",
            **{"lat": 52.0, "lon": 0.5, "alt_baro": 12000, "gs": 250.0, "track": 45.0, "seen": 1.5},
            **{"position_source": "solver_adsb_seed", "multinode": False, "n_nodes": 1, "node_refs": [_REF_B]},
            "is_anomalous": False,
            "trail": [
                {"ts": 1762041598.0, "lat": 51.98, "lon": 0.48, "alt_baro": 11800.0},
                {"ts": 1762041599.0, "lat": 51.99, "lon": 0.49},
                {"ts": 1762041600.0, "lat": 52.0, "lon": 0.5, "alt_baro": 12000.0},
            ],
        }
    ]
    assert index.lookup(callsign=" baw12 ") == [
        {
            **{"hex": "4ca1b2", "flight": "BAW12", "lat": 51.47, "lon": -0.45, "alt_baro": 4000, "gs": 180.0},
            **{"track": 90.0, "seen": 1.0, "position_source": "adsb_single_node", "multinode": False},
            **{"n_nodes": 1, "node_refs": [_REF_A], "is_anomalous": False, "trail": []},
        }
    ]


def test_the_index_follows_each_flush(nodes):
    _flush_once(_frame())
    first = live_index()
    assert live_index() is first
    moved = _ac("ba1234", "tisb_other", "solver_adsb_seed", 52.1, 0.6, 12000, 250.0, node_id=_B)
    _flush_once({"now": 1762041601.0, "messages": 1, "aircraft": [moved]})
    second = live_index()
    assert second.now == 1762041601.0
    assert [(row["hex"], row["lat"]) for row in second.search(LiveFilters())] == [("ba1234", 52.1)]
