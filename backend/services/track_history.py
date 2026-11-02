"""Solver-track history for the keyed GET /api/v1/history.

Positions from the stored track Parquet, by hex and/or area, between start and
end, in the live search's readsb row shape, paged with an opaque cursor.

The rows served are exactly those tracks_store.query_tracks publishes for the
window under the real fleet (start <= solve_ts_ms < end, the box, and every
private-contributor rule, read through tracks_store.published_real_hours), less
those that neither carry nor derive a hex asked for. A row carries its adsb_hex,
stripped and lowercased, or, without one, id_utils.multinode_hex_from_key of
its track_key, the hex the live feed showed for the same target; a hex query
finds it by either, since the feed shows the key-derived hex for an
ADS-B-assisted solve. A row with neither has no hex and is never served: files
written before track_key existed, and records from a tool that carries neither.

Order: the window's UTC hours of solve time, ascending; within an hour, by
frame_ts_ms, solve_ts_ms, track_key (null last), then by the row's own content
(_ORDER), never by the order files are read in, so a day compacted between two
requests pages the same. Within an hour this refines query_tracks' frame_ts_ms,
solve_ts_ms order. frame_ts_ms is the node's clock, with no bound on its
distance from solve_ts_ms (the server's), so one frame-first order over the
whole window could not be read an hour at a time.

A row's key is (frame_ts_ms, solve_ts_ms, track_key, row), ``row`` being its
0-based position among the hour's rows equal on the first three. The cursor is
the last served row's key, versioned, as base64url JSON; the next page starts
strictly after it, in its hour. Hours are read one at a time from the cursor's,
so one hour's rows are held at most and a seven-day span never sits in memory,
and one row past the page is looked for, so next_cursor is None exactly when
nothing follows. Rows become dicts only for the page served.
"""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Collection, Container
from typing import Any, NamedTuple

import pyarrow as pa
import pyarrow.compute

from services import tracks_store
from services.id_utils import multinode_hex_from_key
from services.tracks_store import BBox

# pyarrow generates its compute functions at import, out of a type checker's sight.
pc: Any = pyarrow.compute

HISTORY_PAGE_DEFAULT = 1_000
HISTORY_PAGE_MAX = 10_000

_HOUR_MS = 3_600_000
_CURSOR_VERSION = 1
# Far longer than any cursor a page gives.
_CURSOR_MAX_CHARS = 1024
_INT64_LIMIT = 2**63

# The key's columns, then the content that orders rows equal on them: rows
# still equal after all of it are served alike.
_KEY = ("frame_ts_ms", "solve_ts_ms", "track_key")
_ORDER = [
    (name, "ascending", "at_end")
    for name in (
        *_KEY,
        "lat",
        "lon",
        "alt_m",
        "vel_east_ms",
        "vel_north_ms",
        "vel_up_ms",
        "n_nodes",
        "pos_sigma_m",
        "is_anomalous",
        "adsb_hex",
        "contributing_node_ids",
        "contributing_node_refs",
    )
]
# The live feed's conversions (services/aircraft_feed.py, multinode_to_aircraft).
_M_PER_FT = 0.3048
_KT_PER_MS = 1.94384


class HistoryQueryError(ValueError):
    """A history request that cannot be answered as asked; its message says why."""


class SpanTooLong(HistoryQueryError):
    """start to end spans more than ``max_span_s``, the caller's cap."""

    def __init__(self, max_span_s: int) -> None:
        super().__init__(f"start to end may span at most {max_span_s} seconds")
        self.max_span_s = max_span_s


class BadCursor(HistoryQueryError):
    """A cursor that no page of this window gave."""


class HistoryPage(NamedTuple):
    rows: list[dict]
    next_cursor: str | None


class _Key(NamedTuple):
    frame_ts_ms: int
    solve_ts_ms: int
    track_key: str | None
    row: int


def query_history(
    start_ms: int,
    end_ms: int,
    *,
    max_span_s: int,
    private: Container[str],
    hexes: Collection[str] | None = None,
    bbox: BBox | None = None,
    limit: int = HISTORY_PAGE_DEFAULT,
    cursor: str | None = None,
) -> HistoryPage:
    """One page of the real fleet's published solves with start_ms <= solve_ts_ms < end_ms.

    ``private`` is services.publication.private_node_ids(), read by the caller
    once per request. ``hexes`` (case-insensitive) and ``bbox`` filter, and at
    least one is needed; a row matches a hex by its adsb_hex or by the hex of
    its track_key. ``cursor`` is a previous page's next_cursor, for the
    same window and filters.

    Raises HistoryQueryError, with a message for the caller to show, when the
    window is empty or reversed, the limit is outside 1..HISTORY_PAGE_MAX,
    neither filter is given or no hex is named; its SpanTooLong when the window
    is longer than ``max_span_s`` seconds; its BadCursor when the cursor cannot
    be read or lies outside the window. Raises cold_store.ColdStoreError, as
    query_tracks does, when an hour it reads is kept only in object storage and
    cannot be loaded.
    """
    if not 1 <= limit <= HISTORY_PAGE_MAX:
        raise HistoryQueryError(f"limit must be from 1 to {HISTORY_PAGE_MAX}")
    if start_ms >= end_ms:
        raise HistoryQueryError("end must be after start")
    if end_ms - start_ms > max_span_s * 1000:
        raise SpanTooLong(max_span_s)
    if hexes is None and bbox is None:
        raise HistoryQueryError("give a hex, an area, or both")
    wanted = None if hexes is None else _wanted(hexes)
    after = None if cursor is None else _decode(cursor, start_ms, end_ms)

    def mask(table: pa.Table) -> pa.ChunkedArray:
        adsb, from_key = _hex_parts(table)
        if wanted is None:
            return pc.is_valid(pc.coalesce(adsb, from_key))
        return pc.or_(
            pc.fill_null(pc.is_in(adsb, value_set=wanted), False),
            pc.fill_null(pc.is_in(from_key, value_set=wanted), False),
        )

    resume_hour = None if after is None else after.solve_ts_ms // _HOUR_MS * _HOUR_MS
    first = start_ms if resume_hour is None else max(start_ms, resume_hour)
    rows: list[dict] = []
    next_cursor = None
    for hour in tracks_store.published_real_hours(first, end_ms, private=private, bbox=bbox, mask=mask):
        if len(rows) == limit:
            # An hour after the page's last has rows (the reader skips empty
            # ones), and every one of them follows the page.
            return HistoryPage(rows, next_cursor)
        table = hour.table.take(pc.sort_indices(hour.table, sort_keys=_ORDER))
        offset = 0
        if after is not None and hour.start_ms == resume_hour:
            before, same = _position(table, after.frame_ts_ms, after.solve_ts_ms, after.track_key)
            offset = before + min(after.row + 1, same)
        page = table.slice(offset, limit - len(rows))
        if page.num_rows:
            rows.extend(_rows(page, hour.refs))
            next_cursor = _encode(_key_at(table, offset + page.num_rows - 1))
        if offset + page.num_rows < table.num_rows:
            return HistoryPage(rows, next_cursor)
    return HistoryPage(rows, None)


def _wanted(hexes: Collection[str]) -> pa.Array:
    """The hexes asked for, stripped and lowercased as a row's are; HistoryQueryError when none is named."""
    asked = pa.array([hexes] if isinstance(hexes, str) else list(hexes), pa.string())
    normal = pc.utf8_lower(pc.utf8_trim_whitespace(asked))
    named = pc.unique(normal.filter(pc.fill_null(pc.not_equal(normal, ""), False)))
    if not len(named):
        raise HistoryQueryError("hex must name at least one aircraft")
    return named


def _hex_parts(table: pa.Table) -> tuple[pa.ChunkedArray, pa.ChunkedArray]:
    """Each row's adsb_hex, stripped and lowercased, null when empty or absent,
    and the live feed's hex for its track_key (one hash per distinct key), null
    without a key."""
    adsb = pc.utf8_lower(pc.utf8_trim_whitespace(table.column("adsb_hex")))
    adsb = pc.if_else(pc.fill_null(pc.not_equal(adsb, ""), False), adsb, pa.scalar(None, pa.string()))
    keys = table.column("track_key")
    distinct = pc.unique(keys)
    minted = pa.array(
        [None if key is None else multinode_hex_from_key(key) for key in distinct.to_pylist()], pa.string()
    )
    return adsb, pc.take(minted, pc.index_in(keys, value_set=distinct))


def _hexes(table: pa.Table) -> pa.ChunkedArray:
    """Each row's hex: its adsb_hex when that is not empty, else the hex of its
    track_key; null when it has neither."""
    return pc.coalesce(*_hex_parts(table))


def _position(table: pa.Table, frame_ts_ms: int, solve_ts_ms: int, track_key: str | None) -> tuple[int, int]:
    """How many rows of an hour sorted by _ORDER come before (frame_ts_ms,
    solve_ts_ms, track_key) on _KEY, a null track_key last, and how many equal it."""
    frame, solve, keys = (table.column(name) for name in _KEY)
    if track_key is None:
        key_less, key_equal = pc.is_valid(keys), pc.is_null(keys)
    else:
        key_less = pc.fill_null(pc.less(keys, track_key), False)
        key_equal = pc.fill_null(pc.equal(keys, track_key), False)
    same_frame = pc.equal(frame, frame_ts_ms)
    same_solve = pc.and_(same_frame, pc.equal(solve, solve_ts_ms))
    less = pc.or_(
        pc.less(frame, frame_ts_ms),
        pc.or_(pc.and_(same_frame, pc.less(solve, solve_ts_ms)), pc.and_(same_solve, key_less)),
    )
    equal = pc.and_(same_solve, key_equal)
    return pc.sum(less).as_py() or 0, pc.sum(equal).as_py() or 0


def _key_at(table: pa.Table, index: int) -> _Key:
    """The key of row ``index`` of an hour sorted by _ORDER."""
    frame, solve, track_key = (table.column(name)[index].as_py() for name in _KEY)
    before, _ = _position(table, frame, solve, track_key)
    return _Key(frame, solve, track_key, index - before)


def _encode(key: _Key) -> str:
    raw = json.dumps([_CURSOR_VERSION, *key], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(cursor: str, start_ms: int, end_ms: int) -> _Key:
    """The key a cursor carries; BadCursor unless it is one a page of start..end gave."""
    try:
        if len(cursor) > _CURSOR_MAX_CHARS:
            raise ValueError(cursor)
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        version, frame, solve, track_key, row = json.loads(raw)
        if track_key is not None:
            # Arrow compares UTF-8 only: a lone surrogate cannot be a stored key.
            track_key.encode("utf-8")
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise BadCursor("cursor cannot be read") from None
    numbers = (version, frame, solve, row)
    if not (
        all(type(n) is int and -_INT64_LIMIT <= n < _INT64_LIMIT for n in numbers)
        and version == _CURSOR_VERSION
        and row >= 0
        and (track_key is None or type(track_key) is str)
    ):
        raise BadCursor("cursor cannot be read")
    if not start_ms <= solve < end_ms:
        raise BadCursor("cursor is not from this start and end")
    return _Key(frame, solve, track_key, row)


def _rows(table: pa.Table, refs: dict[tuple[str | None, str | None], tuple[str, ...] | None]) -> list[dict]:
    """The served rows of ``table``, in the live search's readsb shape, as plain Python values.

    A field whose value is unknown is left out, as readsb leaves it out:
    alt_baro without alt_m, gs and track without both horizontal velocities.
    """
    names = (
        "frame_ts_ms",
        "lat",
        "lon",
        "alt_m",
        "vel_east_ms",
        "vel_north_ms",
        "n_nodes",
        "pos_sigma_m",
        "contributing_node_ids",
        "contributing_node_refs",
        "is_anomalous",
    )
    columns = [table.column(name).to_pylist() for name in names]
    rows = []
    for hex_id, frame, lat, lon, alt, east, north, n_nodes, sigma, ids, stored, anomalous in zip(
        _hexes(table).to_pylist(), *columns
    ):
        row: dict[str, Any] = {"hex": hex_id, "ts": frame / 1000, "lat": round(lat, 5), "lon": round(lon, 5)}
        feet = None if alt is None else alt / _M_PER_FT
        if feet is not None and math.isfinite(feet):
            row["alt_baro"] = round(feet)
        knots = _knots(east, north)
        if knots is not None:
            row["gs"] = round(knots, 1)
            row["track"] = round(math.degrees(math.atan2(east, north)) % 360, 1)
        row["position_source"] = "multinode_solve"
        row["multinode"] = True
        row["n_nodes"] = n_nodes
        if sigma is not None:
            row["pos_sigma_m"] = round(sigma, 1)
        row["node_refs"] = list(refs[ids, stored] or ())
        row["is_anomalous"] = bool(anomalous)
        rows.append(row)
    return rows


def _knots(east: float | None, north: float | None) -> float | None:
    """Ground speed in knots, as the live feed computes it; None when a component is unknown or it overflows."""
    if east is None or north is None:
        return None
    try:
        knots = math.sqrt(east**2 + north**2) * _KT_PER_MS
    except OverflowError:
        return None
    return knots if math.isfinite(knots) else None
