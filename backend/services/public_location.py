"""Deterministic per-node location fuzzing, applied at the serialization edge.

A receiver node sits at someone's home.  Its true position is needed by the
physics — bistatic delay, association, the solver all resolve geometry against
it — and by nothing else.  This module is the single place that turns a true
receiver position into the one an unauthenticated client is allowed to see.

**One offset per site, not per node.**  The identity hashed is the node's
site: receivers configured at the same coordinates share an offset and are
published at one point, because two independent offsets around one house are
two samples of it and an attacker intersects them.  services/node_sites.py
resolves that identity and explains what the intersection costs.

**Where this belongs.**  Call it at the boundary where bytes leave for a public
client (a JSON payload, a websocket entry, an archive row), never upstream of
it.  ``services/node_pipeline.py`` and everything it feeds must keep the true
coordinates: fuzzing there would move the aircraft, not the operator.  TX sites
are licensed broadcast towers and are never fuzzed.

**The offset.**  ``HMAC-SHA256(salt, frame_message(node_id))`` seeds a bearing
uniform in [0, 360) and a displacement uniform in [NODE_FUZZ_MIN_KM,
NODE_FUZZ_MAX_KM].  The same node id under the same salt and the same bounds
therefore yields the same offset forever, across processes and restarts — a
node that wandered per boot would be averaged back to the truth by anyone
logging the feed, which is the whole attack this defends against.  Keying on
HMAC rather than a plain hash means the offset cannot be recomputed without the
salt, so publishing the algorithm costs nothing.

**Why the bounds are in the HMAC message.**  They are not there to add entropy;
the salt does that.  They are there so that narrowing or widening the donut
re-draws the bearing as well as the distance.  Derived from the node id alone,
the bearing would be an invariant across a bounds change: a node published
under two sets of bounds would appear at two points on the *same ray* from its
true position, and an attacker holding both — the live map and the public
Parquet archive are enough — recovers the receiver exactly by solving two
linear equations, whatever the bounds were narrowed to.  Mixing the bounds in
makes the second frame an independent draw, which is the difference between a
re-fuzz that costs an operator a little privacy and one that costs all of it.
The frame is the pair actually in force, so the re-key is automatic: no
deployment has to remember to rotate NODE_FUZZ_SALT alongside the bounds, and
one that does rotate the salt anyway is no worse off.

The displacement floor is what makes the region a donut rather than a disc.  A
uniform disc puts a meaningful fraction of nodes within a couple of hundred
metres of home; the floor guarantees every node is displaced by at least
NODE_FUZZ_MIN_KM.  The bearing/radius pair is deliberately not area-uniform —
uniformity over the annulus would concentrate nodes near the outer edge and
give an attacker a prior worth having.

**What this does not do.**  A single fuzzed anchor is not anonymity against an
observer who can correlate many independent geometric channels over time.  It
raises the cost of reading an operator's address off the map; it is not a
guarantee.  Anything derived from the true position that is published
unmodified (a range ring, a distance-to-detection, a polygon apex) hands the
offset straight back, which is why callers translate those too.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets

from config.constants import (
    node_fuzz_max_km,
    node_fuzz_min_km,
    node_fuzz_mode,
    node_fuzz_salt,
)
from core.runtime_config import RUNTIME_DIR, runtime_path, write_runtime_file
from services.geo import KM_PER_DEG_LAT, km_per_deg_lon, offset_latlon
from services.node_sites import site_identity

logger = logging.getLogger(__name__)

# Persisted fallback salt.  Lives beside the other runtime state rather than in
# backend/config, because backend/config is the image's read-only template dir
# and a salt written there would be lost on the next deploy — which would
# silently re-randomise every node's offset.
_SALT_FILE = "node_fuzz_salt"

# Public coordinates are emitted at 4 decimals (~11 m).  Beyond that the digits
# only describe the offset arithmetic, and a fuzzed coordinate quoted to 7
# decimals invites the reader to believe it is a survey fix.
_PUBLIC_DECIMALS = 4

# (hmac message, salt, min_km, max_km) → (east_km, north_km).  The salt and
# bounds are in the key so a test (or a re-salted deployment) cannot read a
# stale offset back out.  Bounded by the fleet size, which is bounded by the node
# registry.
_offset_cache: dict[tuple[str, str, float, float], tuple[float, float]] = {}

# The frame every node was published in before the bounds joined the HMAC
# message, kept so that adopting that change does not by itself move a single
# node.  A deployment still on these bounds keeps the offsets it has always
# served; one that has moved off them is in a new frame already and has nothing
# to preserve.  Frozen literals on purpose — they are a historical fact, not a
# default, so they must not follow NODE_FUZZ_{MIN,MAX}_KM_DEFAULT if those are
# ever edited.
_ORIGINAL_FRAME = (1.0, 3.0)

# Separates the node id from the frame label in the HMAC message.  A NUL cannot
# occur in a node id, so no id can spell another id's message and collide with
# its offset.
_FRAME_SEP = "\x00"

# Resolved persisted salt, read once per process.
_file_salt: str | None = None


def _reset_for_tests() -> None:
    """Drop the memoised offsets and salt.  Tests only."""
    _offset_cache.clear()
    global _file_salt
    _file_salt = None


def fuzz_enabled() -> bool:
    """False only when NODE_FUZZ_MODE is explicitly "off"."""
    return node_fuzz_mode() != "off"


def location_uncertainty_km() -> float:
    """Radius a client should draw to represent an honest published position.

    The outer edge of the donut: the true receiver is somewhere within this
    distance of the coordinate served, and the client is told so rather than
    left to infer a precision that is not there.
    """
    return node_fuzz_max_km()


def _persisted_salt() -> str:
    """The runtime-dir salt, generating and storing one on first use.

    A deployment that sets no NODE_FUZZ_SALT still needs offsets that survive a
    restart, so the generated salt goes to disk immediately.  The file is read
    back after writing, so if two processes ever race to create it they
    converge on the same value instead of serving two different maps.
    """
    global _file_salt
    if _file_salt is not None:
        return _file_salt

    path = runtime_path(_SALT_FILE)
    try:
        if not path.exists():
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            write_runtime_file(path, secrets.token_hex(32))
            logger.info("public_location: generated node fuzz salt at %s", path)
        _file_salt = path.read_text(encoding="utf-8").strip()
    except OSError:
        # An unwritable runtime dir must not take the server down, but it must
        # not fall back to serving true coordinates either.  A process-local
        # salt keeps the map self-consistent for this process's lifetime; the
        # log line is the signal that offsets will move on the next restart.
        logger.exception("public_location: could not persist fuzz salt, using a process-local one")
        _file_salt = secrets.token_hex(32)
    return _file_salt


def _salt() -> str:
    """The HMAC salt in force: the configured one, else the persisted one."""
    return node_fuzz_salt() or _persisted_salt()


def _frame_message(node_id: str, min_km: float, max_km: float) -> str:
    """The HMAC message for one node under one pair of bounds.

    The identity is the node's SITE, not the node: nodes configured at the same
    coordinates resolve to one id and therefore to one offset, so a shared
    receive site is published as one point rather than as two samples of itself
    (services/node_sites.py).  A node alone at its coordinates resolves to its
    own id, which is what it has always keyed on.

    The bare identity in the original frame, so that frame's offsets are
    exactly what they have always been; the identity plus a canonical label for
    the bounds in any other, so every change of bounds lands the fleet in a
    frame unrelated to the one it left (see the module docstring on why that
    matters).

    The label is formatted to a fixed precision rather than interpolated raw:
    "1", "1.0" and "1.000" are the same donut and must not be three frames, or
    an innocent edit to backend/.env would re-fuzz the fleet for nothing.  Six
    decimals is a millimetre of donut radius — far below anything a deployment
    would mean to express, and far above float noise.
    """
    identity = site_identity(node_id)
    if (min_km, max_km) == _ORIGINAL_FRAME:
        return identity
    return f"{identity}{_FRAME_SEP}{min_km:.6f}:{max_km:.6f}"


def public_offset_km(node_id: str | None) -> tuple[float, float]:
    """Deterministic (east_km, north_km) displacement for one node.

    (0.0, 0.0) when fuzzing is off.  A node id of None or "" is hashed as the
    empty string rather than passed through unfuzzed: a missing id is a config
    fault, and the safe reading of a config fault is "do not publish the truth".
    """
    if not fuzz_enabled():
        return (0.0, 0.0)

    key_id = node_id or ""
    salt = _salt()
    min_km = node_fuzz_min_km()
    max_km = node_fuzz_max_km()
    # Keyed on the resolved message, not on the node id: a node's site identity
    # can change under it — the first time a site-mate's configuration is seen,
    # say — and a cache keyed on the id would keep serving the offset from
    # before it had one.
    message = _frame_message(key_id, min_km, max_km)
    key = (message, salt, min_km, max_km)
    cached = _offset_cache.get(key)
    if cached is not None:
        return cached

    digest = hmac.new(salt.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    # Two independent 64-bit draws out of the same digest: bytes 0-7 pick the
    # bearing, bytes 8-15 the displacement.  Dividing by 2**64 lands in [0, 1),
    # so the bearing never repeats 0° as 360° and the displacement never
    # exceeds max_km.
    bearing_deg = (int.from_bytes(digest[:8], "big") / 2**64) * 360.0
    distance_km = min_km + (int.from_bytes(digest[8:16], "big") / 2**64) * (max_km - min_km)

    bearing_rad = math.radians(bearing_deg)
    offset = (distance_km * math.sin(bearing_rad), distance_km * math.cos(bearing_rad))
    _offset_cache[key] = offset
    return offset


def public_latlon(lat, lon, node_id: str | None) -> tuple[float, float]:
    """The coordinate to publish for a node sitting at (lat, lon).

    Returns the inputs untouched — not even rounded — when fuzzing is off or
    when either coordinate is not a usable number, so a disabled deployment and
    a broken config are both exactly pass-through.
    """
    if not fuzz_enabled():
        return (lat, lon)
    if not _is_num(lat) or not _is_num(lon):
        return (lat, lon)

    east_km, north_km = public_offset_km(node_id)
    fuzzed_lat, fuzzed_lon = offset_latlon(float(lat), float(lon), east_km, north_km)
    return (round(fuzzed_lat, _PUBLIC_DECIMALS), round(fuzzed_lon, _PUBLIC_DECIMALS))


def translate_polygon(
    vertices,
    node_id: str | None,
    anchor_lat: float | None = None,
):
    """Rigidly shift a [[lat, lon], …] polygon by the node's offset.

    Rigid is the point.  A coverage polygon drawn around a node has its apex at
    the true receiver and its shape set by what that receiver has actually
    heard; re-deriving the shape around the fuzzed anchor would leak the
    difference between the two.  Every vertex therefore moves by the identical
    (dlat, dlon), so the polygon carries no information about where the node
    is relative to the fuzzed anchor — only the anchor itself moved.

    ``anchor_lat`` is the latitude the longitude conversion is taken at; pass
    the node's true latitude so the apex lands on exactly the coordinate
    ``public_latlon`` publishes for the same node.  It defaults to the first
    vertex's latitude, which for the coverage polygons in this codebase is the
    apex, i.e. the receiver.

    The result is not rounded: rounding each vertex independently would perturb
    them by different amounts and break rigidity, and the vertices arrive
    already rounded by whatever produced them.
    """
    if vertices is None:
        return None
    if not fuzz_enabled():
        return list(vertices)

    verts = list(vertices)
    if not verts:
        return verts

    if anchor_lat is None:
        anchor_lat = verts[0][0]
    if not _is_num(anchor_lat):
        return verts

    east_km, north_km = public_offset_km(node_id)
    dlat = north_km / KM_PER_DEG_LAT
    dlon = east_km / km_per_deg_lon(float(anchor_lat))
    return [[v[0] + dlat, v[1] + dlon] for v in verts]


def public_point_delta(lat, node_id: str | None) -> tuple[float, float]:
    """The (dlat, dlon) that carries a point from the true frame to the public one.

    ``translate_polygon`` works this out for a whole vertex list; this is the
    same arithmetic exposed for callers that hold one point at a time — the
    single-node arc track's icon, its solver estimate and its position trail,
    which have to move with the receiver without ever being handed the
    receiver's coordinates.  ``lat`` is only the latitude the longitude
    conversion is taken at, so passing the point's own latitude is right.

    (0.0, 0.0) when fuzzing is off or ``lat`` is not a usable number, so a
    caller can add the delta unconditionally and get an exact pass-through.

    Deliberately unrounded, and deliberately not ``public_latlon``.  That
    function snaps to 4 decimals because a fuzzed *node anchor* quoted to 7
    invites the reader to believe it is a survey fix; an aircraft position is a
    live estimate that the feed carries at 6 decimals everywhere else, and it
    would be the only coordinate in the payload suddenly an order of magnitude
    coarser than its neighbours.  Rounding the delta itself would be worse
    still: every point of a trail would then move by a slightly different
    amount, which is exactly the rigidity ``translate_polygon`` exists to
    preserve.
    """
    if not fuzz_enabled() or not _is_num(lat):
        return (0.0, 0.0)

    east_km, north_km = public_offset_km(node_id)
    return (north_km / KM_PER_DEG_LAT, east_km / km_per_deg_lon(float(lat)))


def fuzz_node_cfg(node_cfg: dict | None) -> dict | None:
    """A copy of a node config whose rx_lat/rx_lon are the published ones.

    For the serialization paths that hand a whole config to a geometry builder
    (the ambiguity-arc builder, receiver.json).  Building the public artefact
    from the public anchor is what keeps the two consistent: an arc solved
    around the true receiver and drawn beside a fuzzed marker would let anyone
    recover the receiver from the arc's focus.

    TX is copied through untouched, so the published arc keeps the real
    transmitter as its second focus and is a genuine ellipse for the same
    measured bistatic range — just around the wrong receiver.
    """
    if node_cfg is None:
        return None
    if not fuzz_enabled():
        return node_cfg

    rx_lat, rx_lon = public_latlon(
        node_cfg.get("rx_lat"),
        node_cfg.get("rx_lon"),
        node_cfg.get("node_id"),
    )
    return {**node_cfg, "rx_lat": rx_lat, "rx_lon": rx_lon}


def public_node_summary(node_id: str | None, summary):
    """One analytics per-node summary, rewritten for a public client.

    Three things move, all of them derived from the receiver's true position:

    * ``detection_area.rx`` becomes the published coordinate, and carries
      ``location_uncertainty_km`` beside it.  The radius qualifies exactly that
      coordinate, so it travels with it: the client is told the honest distance
      the receiver may be from the point it was given, rather than left to infer
      a precision that is not there.  (The map reads its node positions from
      this payload, not from /api/radar/nodes, so the same field on the nodes
      block never reached it.)  ``tx`` moves by neither.
    * ``empirical_coverage.polygon`` is translated rigidly by the same offset.
      Its apex is the receiver at 5 decimals, so leaving it alone would publish
      the truth beside the fuzzed marker and make the fuzz decorative.
    * ``detection_area.furthest_detections`` is dropped entirely.  Each entry
      is a real aircraft fix plus its distance from the true receiver, which is
      a ranging circle per entry: three of them intersect at the receiver,
      whatever the published anchor says.  No frontend or dashboard surface
      reads it.

    Returns copies — the analytics manager hands out its own cached summary
    dicts, and mutating one would corrupt the state the pipeline reads.
    """
    if not fuzz_enabled() or not isinstance(summary, dict):
        return summary

    out = summary
    rx_lat = None
    area = out.get("detection_area")
    if isinstance(area, dict):
        area_out = {k: v for k, v in area.items() if k != "furthest_detections"}
        rx = area.get("rx")
        if isinstance(rx, dict):
            rx_lat = rx.get("lat")
            pub_lat, pub_lon = public_latlon(rx_lat, rx.get("lon"), node_id)
            area_out["rx"] = {
                **rx,
                "lat": pub_lat,
                "lon": pub_lon,
                "location_uncertainty_km": location_uncertainty_km(),
            }
        out = {**out, "detection_area": area_out}

    coverage = out.get("empirical_coverage")
    if isinstance(coverage, dict) and coverage.get("polygon"):
        out = {
            **out,
            "empirical_coverage": {
                **coverage,
                "polygon": translate_polygon(coverage["polygon"], node_id, anchor_lat=rx_lat),
            },
        }
    return out


def public_cross_node(cross_node: dict) -> dict:
    """The cross-node analysis block, rewritten for a public client.

    ``coverage_suggestions`` goes to ``[]``.  Each entry carries a
    ``test_point`` at 5 decimals, one for each of eight fixed compass bearings
    at a fixed 80 km radius from the MEAN of the fleet's true receiver
    positions — so a single entry, read with its own ``bearing_deg``, gives
    that mean back exactly.  For a small deployment the mean is one or two
    operators' homes; for a large one it is a hard anchor to fit every other
    channel against.

    Dropped rather than translated, unlike everything else in this module.  A
    fuzzed anchor works because there is one node behind it and its offset is
    deterministic; an aggregate has no single offset to apply.  Recomputing the
    suggestions from the published anchors would produce a different set — the
    published positions are scattered by up to NODE_FUZZ_MAX_KM in independent
    directions, so the gaps they imply are not the gaps the fleet actually has
    — and a wrong siting recommendation served as a right one is worse than no
    recommendation.

    The other two keys stay.  ``pair_overlaps`` is node ids plus delay-bin
    statistics with no coordinates (the same argument routes/analytics.py's
    overlaps docstring makes at length), and ``blocked_nodes`` is a list of
    node ids.

    Nothing in the frontend reads the suggestions today, so this costs the
    product nothing now.  They exist for network planning, which is an
    authenticated concern: when there is a planning surface behind
    ``require_admin`` it can serve the true ones from
    ``get_cross_node_analysis()`` directly, which is where they still live.
    """
    if not fuzz_enabled() or not isinstance(cross_node, dict):
        return cross_node
    return {**cross_node, "coverage_suggestions": []}


def public_node_summaries(summaries: dict) -> dict:
    """public_node_summary() across a {node_id: summary} map."""
    if not fuzz_enabled():
        return summaries
    return {nid: public_node_summary(nid, summary) for nid, summary in summaries.items()}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
