"""Public output APIs — solver aircraft and ground truth.

These endpoints expose the server's current aircraft state and ground-truth
data in a clean, documented JSON format suitable for external consumers.

Endpoints
---------
GET /api/v1/solver/aircraft              – All solver-positioned aircraft
GET /api/v1/solver/aircraft?real_only=1  – Real (non-synthetic) nodes only
GET /api/v1/ground-truth/aircraft        – Simulation ground truth trails
GET /api/v1/ground-truth/real            – Real ground truth (OpenSky ADS-B positions)
GET /api/v1/docs                         – HTML documentation

The three data reads take an API key or an entitled session
(core/data_access.py). Only internal access, an administrator or the radar
key, is shown the synthetic fleet; every other caller gets the real nodes alone.
"""

import time
from collections.abc import Callable
from typing import Any

import orjson
from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, Response

from core import state
from core.data_access import Principal, require_data_access
from services import node_refs
from services.adsb_regions import Region, is_usable, regions_covering

router = APIRouter(tags=["aircraft"])


def real_node_ids() -> set:
    """Return the set of node IDs that are NOT synthetic.

    The one test of "real node" for every read that serves a caller the real
    fleet alone: the same as the real-only aircraft feed and analytics variant
    apply when they are built. A published ref goes through
    node_refs.id_for_identity first; a ref's own prefix says nothing.

    Locked: this is a Python-level comprehension over a dict the TCP handler
    and the pruner mutate from other threads — every other multi-key reader
    takes the lock, and an unguarded resize mid-iteration is a 500 on
    /api/v1/solver/aircraft.
    """
    with state.connected_nodes_lock:
        return {nid for nid, info in state.connected_nodes.items() if not info.get("is_synthetic", True)}


def once_per_source[T](build: Callable[[bytes], T]) -> Callable[[bytes], T]:
    """`build`, run once per cached payload and its answer kept until the next.

    The cached payloads on state are bytes, replaced by a new object on every
    refresh, so the object is the version: the last source is held and
    compared by identity, and a read between two refreshes costs no parse.
    """
    last: tuple[bytes, T] | None = None

    def derived(source: bytes) -> T:
        nonlocal last
        if last is None or last[0] is not source:
            last = (source, build(source))
        return last[1]

    return derived


def _format_aircraft(ac: dict) -> dict:
    return {
        "hex": ac.get("hex"),
        "lat": ac.get("lat"),
        "lon": ac.get("lon"),
        "alt_baro": ac.get("alt_baro"),
        "gs": ac.get("gs"),
        "track": ac.get("track"),
        "position_source": ac.get("position_source"),
        "multinode": ac.get("multinode", False),
        "n_nodes": ac.get("n_nodes", 1),
        "contributing_node_refs": ac.get("contributing_node_refs", []),
        "flight": ac.get("flight"),
        "node_ref": ac.get("node_ref"),
        "target_class": ac.get("target_class"),
        "delay_us": ac.get("delay_us"),
        "doppler_hz": ac.get("doppler_hz"),
        # Only claimed-ADS-B entries (``adsb_single_node``/``adsb_multi_node``)
        # carry this; null everywhere else.
        "adsb_fix_age_s": ac.get("adsb_fix_age_s"),
        "rms_delay": ac.get("rms_delay"),
        "rms_doppler": ac.get("rms_doppler"),
        "recent_positions": ac.get("recent_positions", []),
    }


@router.get("/api/v1/solver/aircraft")
async def solver_aircraft(
    real_only: bool = Query(False, description="Return only aircraft detected by real (non-synthetic) nodes"),
    principal: Principal = Depends(require_data_access),
):
    """Return all aircraft the solver is currently tracking.

    Each entry includes the solver's position estimate, position source,
    velocity, and detection quality metrics.

    **position_source** values:
    - ``solver_adsb_seed`` — LM solver converged using ADS-B position as initial guess
    - ``solver_single_node`` — LM solver converged without ADS-B
    - ``single_node_ellipse_arc`` — solver did not converge; position is midpoint of bistatic delay ellipse arc
    - ``multinode_solve`` — position solved from ≥2 node detections (highest accuracy)
    - ``adsb_single_node`` — exactly one node is claiming this transponder; position is its ADS-B fix dead-reckoned to now, not a solve
    - ``adsb_multi_node`` — two or more nodes are claiming this transponder but no multinode solve for it is published; position is its ADS-B fix dead-reckoned to now, not a solve

    **Query params:**
    - ``real_only=true`` — filter to aircraft detected by real hardware nodes only (excludes simulated fleet);
      always on for a key or a session
    """
    # Served beyond the owners, so it reads the redacted feed: a node whose
    # owner registered it private is absent here for the same reason it is
    # absent from the websocket payloads (services/publication.py).
    data = state.latest_aircraft_json_public
    now = time.time()
    # The synthetic fleet is for internal access alone; anyone else asking
    # for it is answered as real_only, and the body says so.
    real_only = real_only or not principal.internal

    if real_only:
        # The feed is published under node_ref, so the connected-node ids are
        # translated before the match rather than compared raw; a node with no
        # ref is not in the payload to be matched either (services/node_refs.py).
        real_refs = {ref for ref in (node_refs.public_identity(nid) for nid in real_node_ids()) if ref}
        aircraft_list = [
            ac
            for ac in data.get("aircraft", [])
            if ac.get("node_ref") in real_refs
            or (ac.get("multinode") and any(ref in real_refs for ref in ac.get("contributing_node_refs", [])))
        ]
    else:
        aircraft_list = data.get("aircraft", [])

    aircraft_out = [_format_aircraft(ac) for ac in aircraft_list]

    body = orjson.dumps(
        {
            "timestamp": now,
            "count": len(aircraft_out),
            "real_only": real_only,
            "aircraft": aircraft_out,
        },
        option=orjson.OPT_SERIALIZE_NUMPY,
    )
    return Response(content=body, media_type="application/json")


@router.get("/api/v1/ground-truth/aircraft")
async def ground_truth_aircraft(principal: Principal = Depends(require_data_access)):
    """Return simulation ground truth positions and metadata for all aircraft.

    Ground truth is pushed by the fleet orchestrator (simulation) every 2 s via
    ``POST /api/test/ground-truth/push``.  Each entry has a 30-point trail
    and metadata (object type, anomaly flag, speed, heading).

    This endpoint is only meaningful when the simulation fleet is running.
    For real ground truth from live hardware nodes, use ``/api/v1/ground-truth/real``.
    The simulation is the synthetic fleet's world, so a key or a session gets
    the envelope with no aircraft in it.
    """
    now = time.time()
    aircraft_out = []
    simulated = list(state.ground_truth_trails.items()) if principal.internal else []
    for hex_code, trail in simulated:
        if not trail:
            continue
        meta = state.ground_truth_meta.get(hex_code, {})
        aircraft_out.append(
            {
                "hex": hex_code,
                "object_type": meta.get("object_type"),
                "is_anomalous": meta.get("is_anomalous", False),
                "speed_ms": meta.get("speed_ms"),
                "heading": meta.get("heading"),
                "has_adsb": meta.get("has_adsb", False),
                "adsb_callsign": meta.get("adsb_callsign"),
                "anomaly_event": meta.get("anomaly_event"),
                # A transponder that is silent right now (simulator outage) — the
                # aircraft still has ADS-B (has_adsb stays true), it is just not
                # broadcasting, which is what the known-track hold is measured on.
                "adsb_silent": meta.get("adsb_silent", False),
                "trail": list(trail)[-30:],
            }
        )

    body = orjson.dumps(
        {
            "timestamp": now,
            "count": len(aircraft_out),
            "source": "simulation",
            "aircraft": aircraft_out,
        },
        option=orjson.OPT_SERIALIZE_NUMPY,
    )
    return Response(content=body, media_type="application/json")


def _published_truth_regions() -> list[Region] | None:
    """The ground the published nodes account for, or None when every
    position fetched may be served.

    The truth is fetched in regions built around the receivers
    (services/tasks/periodic.py), private ones included, so a private node far
    from any published one is the centre of a disc of aircraft served here, and
    its position can be read back from where they are. While a private node has
    a position, only aircraft inside the regions the other nodes would have
    asked for are served. Every node that is not private counts, connected or
    not and synthetic or not: it is on the public map, so what lies around it
    says nothing new.
    """
    withheld = node_refs.withheld_from_public()
    published: list[tuple[Any, Any]] = []
    private: list[tuple[Any, Any]] = []
    with state.connected_nodes_lock:
        for node_id, info in state.connected_nodes.items():
            config = info.get("config") or {}
            position = (config.get("rx_lat"), config.get("rx_lon"))
            (private if node_id in withheld else published).append(position)
    if not regions_covering(private):
        return None
    return regions_covering(published)


def _covered(entry: dict, regions: list[Region]) -> bool:
    lat, lon = entry.get("lat"), entry.get("lon")
    if not (isinstance(lat, int | float) and isinstance(lon, int | float) and is_usable(lat, lon)):
        return False
    return any(region.covers(lat, lon) for region in regions)


@router.get("/api/v1/ground-truth/real", dependencies=[Depends(require_data_access)])
async def ground_truth_real():
    """Return real ground truth aircraft positions from the external ADS-B feed.

    The server polls adsb.lol (or OpenSky where authorised) every 2 minutes and
    caches positions for aircraft within ±1° of all real hardware nodes.  This
    provides an independent position source to validate solver output against
    known ADS-B transponder data.

    Each entry contains:
    - ``hex`` — ICAO 24-bit address
    - ``lat``, ``lon`` — last known position (degrees)
    - ``alt_m`` — barometric altitude (metres)
    - ``velocity`` — ground speed (m/s)
    - ``heading`` — track angle (degrees)
    - ``captured_at`` — when the position was measured (epoch seconds)

    **Usage:** Compare solver output from ``/api/v1/solver/aircraft?real_only=true``
    against these positions to measure geolocation accuracy on real traffic.
    """
    now = time.time()
    regions = _published_truth_regions()
    aircraft_out = []
    for icao, entry in list(state.external_adsb_cache.items()):
        if regions is not None and not _covered(entry, regions):
            continue
        aircraft_out.append(
            {
                "hex": icao,
                "lat": entry.get("lat"),
                "lon": entry.get("lon"),
                "alt_m": entry.get("alt_m"),
                "velocity": entry.get("velocity"),
                "heading": entry.get("heading"),
                # Per-entry, because the envelope timestamp is when this
                # response was built and these positions are older than that
                # by design.  A caller measuring solver accuracy against them
                # cannot age them otherwise.
                "captured_at": (entry.get("last_seen_ms") or 0) / 1000.0,
            }
        )

    body = orjson.dumps(
        {
            "timestamp": now,
            "count": len(aircraft_out),
            # Still names OpenSky for every entry whatever fed it.  Correcting
            # that needs the per-entry provenance field 86cb9btw9 owns; this is
            # a published field, so it does not change on the way past.
            "source": "opensky_network",
            "aircraft": aircraft_out,
        },
        option=orjson.OPT_SERIALIZE_NUMPY,
    )
    return Response(content=body, media_type="application/json")


# ── HTML docs ─────────────────────────────────────────────────────────────────

_DOCS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RETINA API v1 — Documentation</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e2e8f0;line-height:1.6}
  .wrap{max-width:900px;margin:0 auto;padding:40px 24px}
  h1{font-size:2rem;font-weight:700;color:#7dd3fc;margin-bottom:4px}
  h1 span{color:#94a3b8;font-size:1rem;font-weight:400;margin-left:10px}
  .subtitle{color:#64748b;margin-bottom:40px;font-size:.9rem}
  h2{font-size:1.25rem;font-weight:600;color:#e2e8f0;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid #1e293b}
  h3{font-size:1rem;font-weight:600;color:#7dd3fc;margin:20px 0 8px}
  .endpoint{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:20px 24px;margin-bottom:20px}
  .method{display:inline-block;background:#166534;color:#86efac;font-size:.75rem;font-weight:700;padding:2px 8px;border-radius:4px;font-family:monospace;margin-right:10px}
  .path{font-family:'SF Mono',Consolas,monospace;font-size:1rem;color:#e2e8f0}
  .desc{color:#94a3b8;margin:10px 0 14px;font-size:.9rem}
  .param{background:#0f172a;border-radius:6px;padding:10px 14px;margin-bottom:8px;font-size:.85rem}
  .param-name{font-family:monospace;color:#fbbf24}
  .param-type{color:#64748b;margin:0 6px}
  .param-desc{color:#94a3b8}
  .field-table{width:100%;border-collapse:collapse;font-size:.82rem;margin-top:8px}
  .field-table th{text-align:left;padding:6px 10px;background:#0f172a;color:#64748b;font-weight:600}
  .field-table td{padding:6px 10px;border-bottom:1px solid #1e293b;color:#cbd5e1}
  .field-table td:first-child{font-family:monospace;color:#fbbf24}
  .example{background:#020617;border:1px solid #1e293b;border-radius:6px;padding:14px 16px;margin-top:12px;overflow-x:auto}
  .example pre{font-family:'SF Mono',Consolas,monospace;font-size:.78rem;color:#86efac;white-space:pre}
  .tag{display:inline-block;font-size:.7rem;padding:1px 6px;border-radius:3px;margin-left:8px;font-weight:600;vertical-align:middle}
  .tag-sim{background:#1e3a5f;color:#7dd3fc}
  .tag-real{background:#14532d;color:#86efac}
  a{color:#7dd3fc;text-decoration:none}
  a:hover{text-decoration:underline}
  .note{background:#1e293b;border-left:3px solid #7dd3fc;padding:10px 14px;border-radius:0 6px 6px 0;font-size:.85rem;color:#94a3b8;margin-top:12px}
</style>
</head>
<body>
<div class="wrap">
  <h1>RETINA API <span>v1</span></h1>
  <p class="subtitle">Passive Radar Solver Output &amp; Ground Truth — <strong>api.retina.fm</strong></p>

  <h2>Overview</h2>
  <p style="color:#94a3b8;font-size:.9rem;margin-bottom:12px">
    Every GET endpoint on this page takes an API key, sent as
    <code style="color:#fbbf24">x-apikey</code> or <code style="color:#fbbf24">Authorization: Bearer</code>;
    the websocket takes it as <code style="color:#fbbf24">?apikey=</code>. A key is served the real-hardware nodes only.
    The endpoints above the Keyed Data API section answer JSON
    (<code style="color:#fbbf24">Content-Type: application/json</code>).
    Base URL: <code style="color:#fbbf24">https://api.retina.fm</code>
  </p>

  <table class="field-table" style="margin-bottom:0">
    <tr><th>Endpoint</th><th>Description</th><th>Source</th></tr>
    <tr><td><a href="/api/v1/solver/aircraft">/api/v1/solver/aircraft</a></td><td>All solver-positioned aircraft</td><td>All nodes</td></tr>
    <tr><td><a href="/api/v1/solver/aircraft?real_only=true">/api/v1/solver/aircraft?real_only=true</a></td><td>Real-hardware nodes only</td><td>Real nodes</td></tr>
    <tr><td><a href="/api/v1/ground-truth/aircraft">/api/v1/ground-truth/aircraft</a></td><td>Simulation ground truth (physics layer)</td><td>Simulation</td></tr>
    <tr><td><a href="/api/v1/ground-truth/real">/api/v1/ground-truth/real</a></td><td>Real ground truth (OpenSky ADS-B)</td><td>OpenSky Network</td></tr>
  </table>

  <h2>Solver Aircraft <span class="tag tag-sim">SIM</span><span class="tag tag-real">REAL</span></h2>

  <div class="endpoint">
    <span class="method">GET</span><span class="path">/api/v1/solver/aircraft</span>
    <p class="desc">Returns all aircraft currently tracked and positioned by the passive radar solver. Each entry includes the solved lat/lon, position quality metadata, and ADS-B enrichment when available.</p>

    <h3>Query Parameters</h3>
    <div class="param">
      <span class="param-name">real_only</span><span class="param-type">bool, default false</span>
      <span class="param-desc">— When true, returns only aircraft detected by real hardware nodes (excludes the simulated fleet). Use this to query app.retina.fm live data.</span>
    </div>

    <h3>Response Fields (per aircraft)</h3>
    <table class="field-table">
      <tr><th>Field</th><th>Type</th><th>Description</th></tr>
      <tr><td>hex</td><td>string</td><td>ICAO hex or synthetic ID (e.g. <code>A1B2C3</code> or <code>pr1a2b</code>)</td></tr>
      <tr><td>lat / lon</td><td>float</td><td>Solver-derived position (degrees)</td></tr>
      <tr><td>alt_baro</td><td>int</td><td>Altitude (feet, from ADS-B when available)</td></tr>
      <tr><td>gs</td><td>float</td><td>Ground speed (knots)</td></tr>
      <tr><td>track</td><td>float</td><td>Track angle (degrees, 0 = North)</td></tr>
      <tr><td>position_source</td><td>string</td><td>How position was derived — see values below</td></tr>
      <tr><td>multinode</td><td>bool</td><td>True if solved from ≥2 nodes (highest accuracy)</td></tr>
      <tr><td>n_nodes</td><td>int</td><td>Number of contributing nodes</td></tr>
      <tr><td>contributing_node_refs</td><td>array</td><td>Public references of the nodes behind a multinode solve; empty for single-node entries</td></tr>
      <tr><td>flight</td><td>string</td><td>Callsign / flight number</td></tr>
      <tr><td>node_ref</td><td>string</td><td>Public reference of the detecting node (e.g. <code>nde4f2k9xq7m3b8</code>)</td></tr>
      <tr><td>target_class</td><td>string</td><td><code>aircraft</code>, <code>drone</code>, or null</td></tr>
      <tr><td>delay_us</td><td>float</td><td>Bistatic delay of latest detection (µs)</td></tr>
      <tr><td>doppler_hz</td><td>float</td><td>Doppler shift of latest detection (Hz)</td></tr>
      <tr><td>adsb_fix_age_s</td><td>float</td><td>Age of the anchoring ADS-B fix (s) — <code>adsb_single_node</code>/<code>adsb_multi_node</code> entries only, null otherwise</td></tr>
      <tr><td>rms_delay</td><td>float</td><td>RMS delay residual from LM solver (µs) — lower is better</td></tr>
      <tr><td>rms_doppler</td><td>float</td><td>RMS Doppler residual from LM solver (Hz)</td></tr>
      <tr><td>recent_positions</td><td>array</td><td>Last 60 [lat, lon, alt_ft, ts] positions</td></tr>
      <tr><td>predecessor_hex</td><td>string</td><td>Present only on a dark <code>multinode_solve</code> entry whose key was minted to replace a key the solver retired for the same aircraft (a hard turn re-keys it). The retired hex's trail has already been moved into <code>recent_positions</code> above; this names it so a client holding its own per-hex history can stitch that too. Absent otherwise.</td></tr>
    </table>

    <h3>position_source Values</h3>
    <table class="field-table">
      <tr><th>Value</th><th>Meaning</th></tr>
      <tr><td>solver_adsb_seed</td><td>LM solver converged; ADS-B used as initial guess. Best accuracy for ADS-B-tagged targets.</td></tr>
      <tr><td>solver_single_node</td><td>LM solver converged without ADS-B seed.</td></tr>
      <tr><td>single_node_ellipse_arc</td><td>Solver did not converge. Position is midpoint of bistatic delay ellipse.</td></tr>
      <tr><td>multinode_solve</td><td>Solved from ≥2 node detections. Independent of ADS-B. Highest geometric accuracy.</td></tr>
      <tr><td>adsb_single_node</td><td>Exactly one node is claiming this transponder. Position is the ADS-B fix dead-reckoned to now along its own speed and track, and dropped once the fix is 10 s old; the radar contribution is the claiming node and its ambiguity arc, not the position.</td></tr>
      <tr><td>adsb_multi_node</td><td>Two or more nodes are claiming this transponder but no multinode solve for it is published. Position as for <code>adsb_single_node</code>; <code>n_nodes</code> and <code>contributing_node_refs</code> name the claiming nodes, and there is no single ambiguity arc.</td></tr>
    </table>

    <div class="example">
<pre>GET https://api.retina.fm/api/v1/solver/aircraft?real_only=true

{
  "timestamp": 1743714161.5,
  "count": 3,
  "real_only": true,
  "aircraft": [
    {
      "hex": "a3f8c1",
      "lat": 33.7753,
      "lon": -84.3963,
      "alt_baro": 34000,
      "gs": 421.5,
      "track": 187.3,
      "position_source": "solver_adsb_seed",
      "multinode": false,
      "n_nodes": 1,
      "flight": "DAL1234",
      "node_ref": "nde4f2k9xq7m3b8",
      "target_class": "aircraft",
      "delay_us": 14.22,
      "doppler_hz": -83.4,
      "rms_delay": 0.31,
      "rms_doppler": 1.07,
      "recent_positions": [[33.78,-84.40,34000,1743714120], ...]
    }
  ]
}</pre>
    </div>
  </div>

  <h2>Simulation Ground Truth <span class="tag tag-sim">SIM</span></h2>

  <div class="endpoint">
    <span class="method">GET</span><span class="path">/api/v1/ground-truth/aircraft</span>
    <p class="desc">Returns the full ground truth state of all simulated physics objects (aircraft, drones, dark targets, anomalous objects). Updated every 2 s by the fleet orchestrator. Use this to validate solver accuracy against known positions.</p>

    <h3>Response Fields (per aircraft)</h3>
    <table class="field-table">
      <tr><th>Field</th><th>Type</th><th>Description</th></tr>
      <tr><td>hex</td><td>string</td><td>Synthetic ICAO identifier</td></tr>
      <tr><td>object_type</td><td>string</td><td><code>aircraft</code>, <code>drone</code>, or <code>dark</code></td></tr>
      <tr><td>is_anomalous</td><td>bool</td><td>True if this object has been flagged as anomalous (high speed, instant accel, etc.)</td></tr>
      <tr><td>speed_ms</td><td>float</td><td>True speed (m/s)</td></tr>
      <tr><td>heading</td><td>float</td><td>True heading (degrees)</td></tr>
      <tr><td>trail</td><td>array</td><td>Last 30 true positions: [lat, lon, alt_m, t_epoch]</td></tr>
    </table>

    <div class="note">
      Compare with <code>/api/v1/solver/aircraft</code> to measure detection rate and position error.
      The server's live validation endpoint <code>POST /api/test/validate</code> does this automatically and
      returns per-source accuracy statistics.
    </div>
  </div>

  <h2>Real Ground Truth (OpenSky) <span class="tag tag-real">REAL</span></h2>

  <div class="endpoint">
    <span class="method">GET</span><span class="path">/api/v1/ground-truth/real</span>
    <p class="desc">Returns real aircraft positions fetched from OpenSky Network every ~2 minutes. These provide independent ADS-B ground truth to validate the solver output from real hardware nodes.</p>

    <h3>Response Fields (per aircraft)</h3>
    <table class="field-table">
      <tr><th>Field</th><th>Type</th><th>Description</th></tr>
      <tr><td>hex</td><td>string</td><td>ICAO 24-bit address</td></tr>
      <tr><td>lat / lon</td><td>float</td><td>ADS-B position (degrees)</td></tr>
      <tr><td>alt_m</td><td>float</td><td>Barometric altitude (metres)</td></tr>
      <tr><td>velocity</td><td>float</td><td>Ground speed (m/s)</td></tr>
      <tr><td>heading</td><td>float</td><td>Track angle (degrees)</td></tr>
    </table>

    <div class="note">
      Cross-reference with <code>/api/v1/solver/aircraft?real_only=true</code>
      to compute haversine error between the solver's radar-derived positions and the OpenSky ADS-B truth.
    </div>
  </div>

  <h2>Validation Endpoint</h2>

  <div class="endpoint">
    <span class="method">POST</span><span class="path">/api/test/validate</span>
    <p class="desc">Submit a ground truth list; the server matches each truth object to the nearest solver aircraft and returns position error statistics broken down by <code>position_source</code>.</p>
    <div class="example">
<pre>POST https://api.retina.fm/api/test/validate
Content-Type: application/json

{
  "ground_truth": [
    {"id": "abc123", "lat": 33.77, "lon": -84.40, "alt_km": 10.5,
     "has_adsb": true, "is_anomalous": false}
  ]
}

Response:
{
  "validation": {"truth_aircraft": 1, "matched": 1, "detection_rate_pct": 100.0, ...},
  "accuracy": {"avg_position_error_km": 0.42, "p95_position_error_km": 1.1, ...},
  "by_source": {"solver_adsb_seed": {"count": 1, "mean_km": 0.42, ...}}
}</pre>
    </div>
  </div>

  <h2>Live Accuracy Statistics</h2>

  <div class="endpoint">
    <span class="method">GET</span><span class="path">/api/radar/accuracy</span>
    <p class="desc">Returns rolling solver-vs-ADS-B accuracy statistics from the last 5000 position fixes. Updated every 30 s.</p>
    <div class="example">
<pre>{
  "n_samples": 2841,
  "mean_km": 0.38,
  "median_km": 0.22,
  "p95_km": 1.41,
  "by_source": {
    "solver_adsb_seed": {"n_samples": 2100, "mean_km": 0.19, ...},
    "solver_single_node": {"n_samples": 741, "mean_km": 0.72, ...}
  }
}</pre>
    </div>
  </div>

  <h2>Keyed Data API</h2>

  <p style="color:#94a3b8;font-size:.9rem;margin-bottom:12px">
    Live aircraft, lookup and track history from the real nodes (never the synthetic fleet), behind an API key.
    The full contract is committed as <code style="color:#fbbf24">contracts/data-v1.openapi.yaml</code>, and
    the routes are also listed in <a href="/openapi.json">/openapi.json</a>.
  </p>

  <table class="field-table" style="margin-bottom:0">
    <tr><th>Endpoint</th><th>Description</th></tr>
    <tr><td>GET /api/v1/aircraft</td><td>Live aircraft, narrowed by the live filters</td></tr>
    <tr><td>GET /api/v1/lookup</td><td>One aircraft by <code>hex</code> or <code>callsign</code>, with its trail (up to 60 points)</td></tr>
    <tr><td>GET /api/v1/history</td><td>Archived positions for a window, paged by cursor</td></tr>
    <tr><td>GET /api/v1/stream</td><td>The live feed as server-sent events</td></tr>
    <tr><td>WS /ws/v1/aircraft</td><td>The live feed over a websocket</td></tr>
  </table>

  <h3>Authentication and limits</h3>
  <div class="endpoint">
    <p class="desc">Send the key as <code>x-apikey: &lt;key&gt;</code> or <code>Authorization: Bearer &lt;key&gt;</code>; websockets take <code>?apikey=&lt;key&gt;</code>. A signed-in entitled account's session cookie also works. Limits are per key, by tier: requests per minute and concurrent streams. A refusal is <code>{"detail": {"reason", "message"}}</code>:</p>
    <table class="field-table">
      <tr><th>Status</th><th>reason</th></tr>
      <tr><td>401</td><td><code>missing_credentials</code>, <code>invalid_key</code></td></tr>
      <tr><td>403</td><td><code>not_entitled</code>, <code>lapsed</code>, <code>grant_ended</code></td></tr>
      <tr><td>429</td><td><code>rate_limited</code> (with <code>Retry-After</code> and <code>X-RateLimit-Limit</code>, <code>-Remaining</code>, <code>-Reset</code>), <code>stream_limit</code> (more streams open than the tier allows)</td></tr>
    </table>
  </div>

  <h3>Live filters</h3>
  <div class="endpoint">
    <p class="desc">Shared by <code>/api/v1/aircraft</code>, <code>/api/v1/stream</code> and the websocket. A refused parameter is a 422.</p>
    <table class="field-table">
      <tr><th>Parameter</th><th>Description</th></tr>
      <tr><td>bbox</td><td><code>south,west,north,east</code>; west greater than east crosses the antimeridian</td></tr>
      <tr><td>lat, lon, radius_nm</td><td>Together: great-circle radius in nautical miles</td></tr>
      <tr><td>alt_min, alt_max</td><td>Altitude, feet</td></tr>
      <tr><td>gs_min, gs_max</td><td>Ground speed, knots</td></tr>
      <tr><td>hex</td><td>Comma-separated, up to 1,000</td></tr>
      <tr><td>callsign</td><td>Prefix, case-insensitive</td></tr>
    </table>
    <div class="note">
      Rows follow readsb's <code>aircraft.json</code> (<code>hex</code>, <code>flight</code>, <code>lat</code>, <code>lon</code>,
      <code>alt_baro</code> ft, <code>gs</code> kt, <code>track</code> deg, <code>seen</code> s) plus <code>position_source</code>,
      <code>multinode</code>, <code>n_nodes</code>, <code>pos_sigma_m</code>, <code>node_refs</code> and <code>is_anomalous</code>.
      Unknown values are omitted.
    </div>
  </div>

  <h3>History</h3>
  <div class="endpoint">
    <p class="desc"><code>start</code> and <code>end</code> (epoch ms or ISO-8601; <code>end</code> is exclusive), narrowed by <code>hex</code> and/or <code>bbox</code>; <code>limit</code> (default 1,000, at most 10,000); <code>cursor</code>, the previous page's <code>next_cursor</code> with the same window and filters. The body is <code>{"aircraft": [...], "next_cursor": string | null}</code>. A window longer than the key's tier allows (604,800 s for administrators and internal services) is a 422 naming the cap; a 503 means an archived day could not be loaded, so retry.</p>
  </div>

  <h3>Websocket</h3>
  <div class="endpoint">
    <p class="desc"><code>/ws/v1/aircraft?apikey=&lt;key&gt;</code>, with the live filters in the query.</p>
    <table class="field-table">
      <tr><th>Direction</th><th>Message</th></tr>
      <tr><td>Server to client</td><td><code>{"type": "aircraft", "now", "aircraft"}</code> at once, on each new flush (about 1 s) and right after a filters change</td></tr>
      <tr><td>Server to client</td><td><code>{"type": "error", "reason": "invalid_filters" | "bad_message", "message"}</code></td></tr>
      <tr><td>Client to server</td><td><code>{"type": "filters", ...}</code> replaces the filters wholly, under the same names; <code>bbox</code> may be a list of four numbers, <code>hex</code> a list, null means no filter. A refused message keeps the old filters and the socket.</td></tr>
    </table>
    <div class="note">
      A connection refused at connect gets an HTTP response to its upgrade, with the status, headers and body a REST call gets, and no socket opens:
      401 or 403 with the refusal reason (<code>missing_credentials</code>, <code>invalid_key</code>, <code>not_entitled</code>, <code>lapsed</code>,
      <code>grant_ended</code>, <code>forbidden_origin</code>), 422 naming the rule a filter in the query breaks, or 429 <code>rate_limited</code> or
      <code>stream_limit</code> with <code>Retry-After</code>. Once open, the socket is closed 1008 with the refusal reason when the 30 s access re-check refuses (key revoked, entitlement lapsed or ended).
    </div>
  </div>

  <h3>Server-sent events</h3>
  <div class="endpoint">
    <p class="desc"><code>/api/v1/stream</code> sends <code>event: aircraft</code> with <code>data: {"now", "aircraft"}</code> at once and on each new flush, and a <code>: keepalive</code> comment after 15 s without a write. When the 30 s re-check refuses, it sends <code>event: error</code> with <code>data: {"reason", "message"}</code> and the stream ends. A <code>stream_limit</code> is a 429 before the stream starts.</p>
  </div>

  <p style="margin-top:48px;color:#475569;font-size:.8rem;text-align:center">
    RETINA Passive Radar Network — api.retina.fm
  </p>
</div>
</body>
</html>"""


@router.get("/api/v1/docs", response_class=HTMLResponse, include_in_schema=False)
async def api_docs():
    """Human-readable HTML documentation for all v1 public API endpoints."""
    return HTMLResponse(content=_DOCS_HTML)
