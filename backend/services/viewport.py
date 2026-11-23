"""The public live map's viewport: /ws/aircraft/live and aircraft-live.json.

The anonymous map is not sent the whole real-node feed. Each reader names the
box it shows, south, west, north and east in degrees (west > east crosses the
antimeridian), and gets only the aircraft and detection arcs inside it. A box
longer than MAP_VIEWPORT_MAX_KM on its long side is cut down around its centre
first, and every frame says which box it was cut to and whether it had to be.

An aircraft is inside when any point the map draws it at is: its ambiguity arc
when that has two points or more, else its position, else the arc's one point
(dashboard/src/pages/map/geo.ts, getAircraftGeometryPoints). An arc is inside
when any of its points is.

Each flush indexes the real-only feed once (publish): every aircraft entry and
arc is serialised once, beside its points, and a reader's frame is joined from
those bytes, so a socket costs a scan and a join, not a serialisation. Sockets
with the same box in one flush share one frame. A socket's viewport messages are
answered at once at most every VIEWPORT_ANSWER_INTERVAL_S, so a client cannot
make the server cut frames as fast as it can send boxes.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

import orjson
from fastapi import WebSocket

from core import state

if TYPE_CHECKING:
    from services.tracks_store import BBox

MAP_VIEWPORT_MAX_KM_DEFAULT = 500.0
# Kilometres per degree of latitude, and of longitude on the equator, on the
# mean Earth radius.
KM_PER_DEG = math.pi * 6371.0088 / 180
# A viewport message is a type and four numbers; anything longer is not one.
VIEWPORT_MESSAGE_MAX_BYTES = 1024
# The least time between two frames a socket is sent at once for its viewport
# messages. A box named sooner is still kept, and the next broadcast carries its
# frame. The map names a box at most every 300 ms (VIEWPORT_SETTLE_MS in
# dashboard/src/pages/map/hooks.ts), so this never holds it back.
VIEWPORT_ANSWER_INTERVAL_S = 0.25
# The clock that interval is measured on.
_clock = time.monotonic

_NUMPY = orjson.OPT_SERIALIZE_NUMPY
# The keys a frame cuts or recounts; the payload's others are copied after them.
_CUT_KEYS = ("now", "messages", "aircraft", "detection_arcs")


def map_viewport_max_km() -> float:
    """MAP_VIEWPORT_MAX_KM, the longest side a box may have, read at call time.

    Unset or blank is the default; anything but a positive, finite number of
    kilometres is the default with a warning. Not bound at import, for the
    reason config/constants.archive_rate_limits() gives: main.py loads
    backend/.env after the route imports.
    """
    raw = os.getenv("MAP_VIEWPORT_MAX_KM")
    if raw is None or not raw.strip():
        return MAP_VIEWPORT_MAX_KM_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        logging.warning(
            "MAP_VIEWPORT_MAX_KM=%r is not a positive number of kilometres, using %s", raw, MAP_VIEWPORT_MAX_KM_DEFAULT
        )
        return MAP_VIEWPORT_MAX_KM_DEFAULT
    return value


def _lon_span(west: float, east: float) -> float:
    return east - west if west <= east else east - west + 360


def _widest_parallel(south: float, north: float) -> float:
    """The latitude a box is widest at: the equator when the box spans it."""
    return 0.0 if south <= 0 <= north else min(abs(south), abs(north))


def _wrap(lon: float) -> float:
    """``lon`` in [-180, 180)."""
    return (lon + 180) % 360 - 180


def clip_bbox(box: BBox) -> tuple[BBox, bool]:
    """``box`` cut to MAP_VIEWPORT_MAX_KM on its long side, and whether it was.

    Its width is measured on its widest parallel. A box within the cap comes
    back as asked. A longer one keeps its centre: the latitude span shrinks to
    the cap, then the longitude span to the cap on the new range's widest
    parallel, and the edges are rounded to six decimals. West lands in
    [-180, 180) and east in (-180, 180], so only a box that crosses the
    antimeridian has west > east, and one that would wrap the globe is
    -180..180.
    """
    from services.tracks_store import BBox

    cap = map_viewport_max_km()
    lon_span = _lon_span(box.west, box.east)
    height_km = (box.north - box.south) * KM_PER_DEG
    width_km = lon_span * KM_PER_DEG * math.cos(math.radians(_widest_parallel(box.south, box.north)))
    if max(height_km, width_km) <= cap:
        return box, False

    lat_span = min(box.north - box.south, cap / KM_PER_DEG)
    mid_lat = (box.south + box.north) / 2
    south, north = mid_lat - lat_span / 2, mid_lat + lat_span / 2
    new_span = min(lon_span, cap / (KM_PER_DEG * math.cos(math.radians(_widest_parallel(south, north)))), 360)
    if new_span >= 360:
        west, east = -180.0, 180.0
    else:
        west = _wrap(box.west + lon_span / 2 - new_span / 2)
        east = west + new_span
        if east > 180:
            east -= 360
    return BBox(round(south, 6), round(west, 6), round(north, 6), round(east, 6)), True


class View(NamedTuple):
    """A box as served: clipped to the cap, and whether it had to be."""

    box: BBox
    clipped: bool


def view_of(box: BBox) -> View:
    return View(*clip_bbox(box))


def viewport_from_message(text: str | None) -> BBox | None:
    """The box in a /ws/aircraft/live client's message, or None for any other message.

    A viewport message is ``{"type": "viewport", "bbox": [south, west, north,
    east]}``, its numbers within the bounds routes/archive_common.parse_bbox
    takes, in at most VIEWPORT_MESSAGE_MAX_BYTES.
    """
    from services.tracks_store import BBox

    if text is None or len(text) > VIEWPORT_MESSAGE_MAX_BYTES:
        return None
    try:
        if len(text.encode()) > VIEWPORT_MESSAGE_MAX_BYTES:
            return None
        message = orjson.loads(text)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("type") != "viewport":
        return None
    bbox = message.get("bbox")
    # type(), not isinstance: a JSON true is a Python int.
    if not (isinstance(bbox, list) and len(bbox) == 4 and all(type(v) in (int, float) for v in bbox)):
        return None
    south, west, north, east = (float(v) for v in bbox)
    # NaN and infinities fail these comparisons too.
    if not (-90 <= south <= north <= 90 and -180 <= west <= 180 and -180 <= east <= 180):
        return None
    return BBox(south, west, north, east)


# ── One flush's frames ───────────────────────────────────────────────────────


def _point(value) -> tuple[float, float] | None:
    """A [lat, lon] pair as floats, its longitude in [-180, 180], or None."""
    try:
        lat, lon = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not -180 <= lon <= 180:
        lon = _wrap(lon)
    return lat, lon


def _points(raw) -> tuple[tuple[float, float], ...]:
    return tuple(p for p in map(_point, raw) if p is not None)


def _aircraft_points(ac: dict) -> tuple[tuple[float, float], ...]:
    arc = ac.get("ambiguity_arc")
    has_arc = isinstance(arc, (list, tuple))
    if has_arc and len(arc) >= 2:
        return _points(arc)
    if ac.get("lat") is not None and ac.get("lon") is not None:
        return _points([(ac["lat"], ac["lon"])])
    return _points(arc[:1]) if has_arc else ()


def _arc_points(arc: dict) -> tuple[tuple[float, float], ...]:
    raw = arc.get("ambiguity_arc")
    return _points(raw) if isinstance(raw, (list, tuple)) else ()


def _inside(box: BBox, points: tuple[tuple[float, float], ...]) -> bool:
    crosses = box.west > box.east
    for lat, lon in points:
        if box.south <= lat <= box.north and (
            (lon >= box.west or lon <= box.east) if crosses else box.west <= lon <= box.east
        ):
            return True
    return False


class _Feed:
    """One flush's real-only feed, its entries serialised once beside their points."""

    def __init__(self, payload: dict, source: bytes):
        # The bytes state holds for this payload; see _latest().
        self.source = source
        self._now = orjson.dumps(payload.get("now", 0), option=_NUMPY)
        self._aircraft = [(orjson.dumps(ac, option=_NUMPY), _aircraft_points(ac)) for ac in payload.get("aircraft", [])]
        self._arcs = [(orjson.dumps(arc, option=_NUMPY), _arc_points(arc)) for arc in payload.get("detection_arcs", [])]
        rest = {key: value for key, value in payload.items() if key not in _CUT_KEYS}
        self._rest = b"," + orjson.dumps(rest, option=_NUMPY)[1:-1] if rest else b""
        self._shared: dict[View, str] = {}

    def frame(self, view: View, *, share: bool) -> str:
        """The frame for ``view``: the payload's keys in its order, the
        aircraft and arcs cut to the box and counted again, then ``viewport``.

        ``share`` keeps it for the other sockets with the same box this flush.
        Only the broadcast shares: a frame built for a new viewport message or
        an HTTP read serves that one reader, so a client cycling through boxes
        cannot grow what is kept.
        """
        text = self._shared.get(view)
        if text is None:
            text = self._cut(view)
            if share:
                self._shared[view] = text
        return text

    def _cut(self, view: View) -> str:
        box = view.box
        aircraft = [raw for raw, points in self._aircraft if _inside(box, points)]
        arcs = [raw for raw, points in self._arcs if _inside(box, points)]
        viewport = orjson.dumps({"bbox": [float(edge) for edge in box], "clipped": view.clipped})
        return b"".join(
            (
                b'{"now":',
                self._now,
                b',"messages":%d,"aircraft":[' % len(aircraft),
                b",".join(aircraft),
                b'],"detection_arcs":[',
                b",".join(arcs),
                b"]",
                self._rest,
                b',"viewport":',
                viewport,
                b"}",
            )
        ).decode()


_EMPTY = _Feed(
    {
        "now": 0,
        "messages": 0,
        "aircraft": [],
        "detection_arcs": [],
        "detecting_nodes": {},
        "ground_truth": {},
        "ground_truth_meta": {},
        "anomaly_hexes": [],
    },
    b"",
)
_feed: _Feed | None = None


def publish(payload: dict, source: bytes) -> None:
    """Index one flush's real-only feed: ``payload`` as published, ``source``
    the bytes of it that state.latest_real_aircraft_json_bytes now holds."""
    global _feed
    _feed = _Feed(payload, source)


def _latest() -> _Feed | None:
    """The last flush's index while its bytes are still state's latest, so
    whatever replaces those (a reset of state) retires the index with them."""
    feed = _feed
    if feed is None or feed.source is not state.latest_real_aircraft_json_bytes:
        return None
    return feed


def frame(box: BBox) -> str:
    """The latest frame for ``box`` once clipped; before any flush, one with nothing in it."""
    return (_latest() or _EMPTY).frame(view_of(box), share=False)


# ── The live sockets' viewports ──────────────────────────────────────────────


@dataclass(slots=True)
class _Viewer:
    view: View
    # Serialises this socket's sends, the broadcast's and the one a new
    # viewport triggers, and each send picks its frame once it holds this, so
    # the last frame on the wire is cut to the latest viewport.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # The _clock time a viewport message was last answered with a frame at once.
    answered_at: float = -math.inf


_viewers: dict[WebSocket, _Viewer] = {}


def watch(ws: WebSocket, box: BBox) -> None:
    """Make ``box``, once clipped, the viewport ``ws`` is sent, replacing its last."""
    view = view_of(box)
    viewer = _viewers.get(ws)
    if viewer is None:
        _viewers[ws] = _Viewer(view)
    else:
        viewer.view = view


def is_watching(ws: WebSocket) -> bool:
    return ws in _viewers


def forget(ws: WebSocket) -> None:
    _viewers.pop(ws, None)


async def send_frame(ws: WebSocket, *, share: bool = True) -> bool:
    """Send ``ws`` the latest frame for its viewport, and say whether one went;
    nothing does without a viewport or a frame."""
    viewer = _viewers.get(ws)
    if viewer is None:
        return False
    async with viewer.lock:
        feed = _latest()
        if feed is None:
            return False
        await ws.send_text(feed.frame(viewer.view, share=share))
        return True


async def answer(ws: WebSocket) -> None:
    """Answer the viewport message ``ws`` just sent with its frame at once,
    unless one was less than VIEWPORT_ANSWER_INTERVAL_S ago.

    Only these answers are spaced out; the broadcast's sends neither wait for
    the interval nor start it.
    """
    viewer = _viewers.get(ws)
    if viewer is None:
        return
    now = _clock()
    if now - viewer.answered_at >= VIEWPORT_ANSWER_INTERVAL_S and await send_frame(ws, share=False):
        viewer.answered_at = now
