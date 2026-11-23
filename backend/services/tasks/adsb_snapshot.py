"""Background task: snapshot the server's ADS-B picture into the raw archive.

Every ADSB_SNAPSHOT_INTERVAL_S this turns the ADS-B the server holds into rows
for one instant (services/adsb_writer.py lists the columns), and every
ADSB_SNAPSHOT_FLUSH_INTERVAL_S it writes the buffered snapshots as one raw part.
It runs only while ADSB_SNAPSHOT_ENABLED.

**What is archived.**  Two sources, both cleared for it:

* the nodes' own ADS-B and the simulator's fleet, from
  state.adsb_fixes_by_reporter, which keeps each reporter's latest fix of each
  hex (source "node");
* ADSBHub traffic polled from adsb.retina.fm, from state.adsb_fallback
  (source "aggregator", world "real").

Nothing else is read for rows.  The OpenSky / adsb.lol truth cache has other
terms and is never archived, and neither is the shared per-hex feed store: its
world "real" records from the simulator's ingest are a relay of adsb.lol.

**Freshness.**  A fix is written only while claiming would still use it: its
capture time within KNOWN_CLAIM_MAX_FIX_AGE_S of the snapshot, either side.
A reporter's fix outside that window is dropped from the map, either side too:
one stamped further ahead would otherwise hold its place against every later
fix of that reporter (note_adsb_reporter keeps the newer) until the clock
reached it, and then be written as current.

**Node rows.**  Only reporters publishable at the snapshot are written:
publishable means not private and with a public identity
(node_refs.publishable), asked once per reporter per snapshot.  A reporter
that is not is never archived, which is stricter than the reader's drop of
nodes private on the day it reads.  While the privacy policy cannot be read
at all, no row from a node is written.

In world "real" each of those reporters' own fix of an aircraft is a row of
its own, attributed to that reporter alone; a reporter whose fixes sit under
two spellings of one hex gives its fresher one.  The reader drops a node
withheld on the day it reads before it picks the freshest fix per aircraft
(services/adsb_store.py), so what it serves is what it would serve had that
node never reported, even when it was made private after its rows were
written: the aircraft stays wherever another public node heard it, from that
node's own fixes, and nothing the withheld node reported is served.  One row
per aircraft would not do.  Attributed to the freshest reporter alone, it
leaves gaps once that node is withheld, and the gaps trace where it
out-heard its neighbours.  Attributed to every reporter, it keeps serving the
withheld node's own fix wherever that was the freshest, and that fix's time
and position trace the node's coverage and its clock.  The cost is a row for
every public node that hears an aircraft (docs/architecture.md).

In world "sim" one row is written per aircraft, with the freshest fix, ties
to the smaller id (the simulator's own fleet, None, first), attributed to
its reporter.  Its reporters are synthetic nodes, which never register a
publication choice and so are never withheld (services/publication.py).

**Aggregator rows: attribution and clipping.**  An ADSBHub region is a disc
centred on its members' TRUE positions (services/adsb_regions.py).  Archived
raw, its traffic draws the disc's sharp edge, and a day of positions fits the
centre to well under the 0.5-1 km fuzz: it hands back a public node's true
position and locates a private node outright.  So a polled fix is written only
inside a disc around the PUBLISHED position of a node in the cell that polled
it, the position the public map shows (public_location.public_latlon).  Every
such fix of an aircraft is written as a row of its own: an aircraft in two
polled cells can have one from each, and the same fix in both answers (same
capture time and position) is written once.  Each row is attributed to exactly
the nodes whose disc holds its fix, among the nodes of the cells whose answers
hold the aircraft.  A node is never listed for another fix's position, nor
from a cell whose answer does not hold the aircraft.  The reader drops the rows
that list no publishable node before it picks the freshest fix per aircraft
(services/adsb_store.py), so a withheld node's fresher fix never hides the one
a public node's disc holds.  Writing only the freshest would hide it, and the
gaps left at that disc's edge would trace the withheld node's coverage.

The clip radius is ADSB_NODE_RANGE_MARGIN_KM less the furthest a published
position can sit from the true one (max_published_offset_km), less the slack
for the service measuring its radius on another earth model.  It is one figure
for every node: a per-node radius would publish each node's offset.  Each
node's true range disc lies inside its cell's polled disc, so its clip disc
does too, and the traffic inside it is complete whatever other nodes the cell
holds.  The reader serves an aggregator row only while a listed node is
publishable, so what it serves is a function of published positions alone.

Three conditions keep "complete" true at the edges:

* a fix is written only through the nodes of the cell that polled it.  Another
  cell's answer covers its own nodes, and filling one node's disc from it
  would draw that cell's edge whenever this cell's answer lags.  A node of
  another cell is listed beside them only when its own cell's answer holds the
  aircraft too, so its disc is complete there.  A cell's own fix is written
  whatever fresher fix another cell holds, so a node's disc holds a row for
  every aircraft its own cell's answer places inside it;
* a node attributes nothing until MEMBER_SETTLE_S after it is first seen in a
  polled cell at its position.  Until then the cell may still hold an answer
  polled without it, over a disc that does not cover its range;
* a node whose published position sits further from its true one than the
  bound (a site snapshot still catching up with a move) attributes nothing.

**Unlocked reads.**  The reporter map is written by the event loop and the
frame workers without a lock, so it is copied before it is walked, and a copy
that a writer resized mid-way is taken again rather than losing the snapshot.
Pruning compares before it pops: only an entry judged stale is removed.  One
race remains: a frame worker that fetched a hex's dict just as the prune pops
it, emptied, writes into a dict nothing holds, and that one fix is lost until
the reporter's next one, about a second later.

**Buffer and flush.**  Each snapshot is one batch in a deque of
ADSB_SNAPSHOT_BUFFER_MAX; when it is full the oldest is dropped and counted.
A flush (services/tasks/track_archive.py's discipline) takes the deque only
when no failed batch is pending, writes one part, and clears the batch only
after the write succeeds, so memory stays within two deques' worth.  The write
runs on the task's executor; the snapshot stays on the event loop.  At
shutdown, once the lifespan has cancelled and awaited its tasks, it writes what
is pending and buffered once more (final_flush_adsb_snapshots), best effort,
whatever other work is still running: only this task's own flush writes these
batches.  It never waits for that flush: one still writing then holds the
lock, and the rows it holds and those buffered behind it are logged as rows
that may not be written.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

from config.constants import (
    ADSB_FALLBACK_INTERVAL_S,
    ADSB_NODE_RANGE_MARGIN_KM,
    ADSB_SNAPSHOT_BUFFER_MAX,
    ADSB_SNAPSHOT_ENABLED,
    ADSB_SNAPSHOT_FLUSH_INTERVAL_S,
    ADSB_SNAPSHOT_INTERVAL_S,
    node_fuzz_max_km,
    node_fuzz_site_km,
)
from core import state
from services import adsb_writer
from services.adsb_regions import KM_PER_NM, MAX_RADIUS_NM, cell_of, is_position_absent, is_usable
from services.adsb_writer import is_finite_number, write_adsb_parquet
from services.geo import KM_PER_DEG_LAT, haversine_km
from services.id_utils import normalize_hex_key
from services.known_claiming import KNOWN_CLAIM_MAX_FIX_AGE_S
from services.node_refs import publishable
from services.public_location import fuzz_enabled, public_latlon
from services.publication import PublicationUnavailable, private_node_ids
from services.tasks.adsb_fallback import positioned_real_nodes
from services.tasks.executor import task_executor

logger = logging.getLogger(__name__)

_TASK = "adsb_snapshot"

_MAX_FIX_AGE_MS = KNOWN_CLAIM_MAX_FIX_AGE_S * 1000.0

# Published coordinates are rounded to 4 decimals, at most 7.9 m off the
# offset point; the flat-earth offset itself is metres closer still.
_PUBLISHED_ROUNDING_KM = 0.01

# adsb.retina.fm measures its radius on an earth model we do not control.  A
# WGS-84 geodesic reads up to 0.45% longer than our haversine at a region's
# size (measured over every latitude and bearing), so its disc can fall short
# of ours by that much; 0.5% of the widest radius a region can ask for covers it.
_POLL_METRIC_SLACK_KM = 0.005 * MAX_RADIUS_NM * KM_PER_NM

# How long a node waits, after it is first seen in a polled cell at its
# position, before it attributes.  An answer polled before the poller saw it
# can still arrive up to one cycle later, plus a request in flight (10 s, over
# the client's 4 s timeouts), and its fixes stay fresh for
# KNOWN_CLAIM_MAX_FIX_AGE_S after their capture.
MEMBER_SETTLE_S = KNOWN_CLAIM_MAX_FIX_AGE_S + ADSB_FALLBACK_INTERVAL_S + 10.0

# A great circle is at least KM_PER_DEG_LAT km per degree of latitude it
# spans, haversine_km's own sphere, so a member further than the clip radius
# in latitude alone cannot hold a fix; a metre over the radius covers rounding.
_CLIP_LAT_SLACK_KM = 0.001

# A writer resizing a dict mid-copy raises; the next copy almost always passes.
_COPY_ATTEMPTS = 8

# One line per this many seconds while the privacy policy cannot be read.
_UNAVAILABLE_LOG_INTERVAL_S = 300.0

_buffer: deque[list[dict]] = deque(maxlen=ADSB_SNAPSHOT_BUFFER_MAX)
_buffer_lock = threading.Lock()
# At most one failed batch, retried before anything newer is taken.
_pending_rows: list[dict] = []
_flush_lock = threading.Lock()
# node id -> ((cell, rx_lat, rx_lon), ts_ms first seen there).  Event loop only.
_member_since: dict[str, tuple[tuple, int]] = {}
_unavailable_logged_at: float | None = None


def _reset_for_tests() -> None:
    """Drop the buffer, the pending batch and the members.  Tests only."""
    global _unavailable_logged_at
    with _buffer_lock:
        _buffer.clear()
    _pending_rows.clear()
    _member_since.clear()
    _unavailable_logged_at = None


def _items(d) -> list:
    """list(d.items()), taken again when another thread resizes d mid-copy."""
    for _ in range(_COPY_ATTEMPTS - 1):
        try:
            return list(d.items())
        except RuntimeError:
            continue
    return list(d.items())


def _is_fresh(ts_ms: int, last_seen_ms) -> bool:
    return is_finite_number(last_seen_ms) and abs(ts_ms - last_seen_ms) <= _MAX_FIX_AGE_MS


def _is_place(lat, lon) -> bool:
    return is_usable(lat, lon) and not is_position_absent(lat, lon)


def _float_or_none(v) -> float | None:
    return float(v) if is_finite_number(v) else None


def _alt_baro(v) -> int | None:
    """Feet as an int32; the literal "ground" is 0, as state.adsb_derived_fields reads it."""
    if v == "ground":
        return 0
    if not is_finite_number(v):
        return None
    alt = int(round(v))
    return alt if -(2**31) <= alt < 2**31 else None


def _row(ts_ms: int, hexn: str, rec: dict, world: str, source: str, source_node_id: str | None) -> dict | None:
    """One archive row from a stored record, or None when its position is unusable."""
    lat, lon = rec.get("lat"), rec.get("lon")
    if not (is_finite_number(lat) and is_finite_number(lon) and _is_place(lat, lon)):
        return None
    flight = rec.get("flight")
    flight = flight.strip() if isinstance(flight, str) else ""
    return {
        "ts_ms": ts_ms,
        "hex": hexn,
        "lat": float(lat),
        "lon": float(lon),
        "alt_baro": _alt_baro(rec.get("alt_baro")),
        "gs": _float_or_none(rec.get("gs")),
        "track": _float_or_none(rec.get("track")),
        "flight": flight or None,
        "seen_ms": int(rec["last_seen_ms"]),
        "world": world,
        "source": source,
        "source_node_id": source_node_id,
    }


def _publishable(reporters: set[str]) -> dict[str, bool] | None:
    """Which node reporters may be archived now; None while the policy cannot be read."""
    if not reporters:
        return {}
    try:
        private = private_node_ids()
    except PublicationUnavailable:
        return None
    return {r: publishable(r, private) for r in reporters}


def _note_policy_unavailable() -> None:
    global _unavailable_logged_at
    state.bump_counter("adsb_snapshot_publication_unavailable")
    now = time.monotonic()
    if _unavailable_logged_at is None or now - _unavailable_logged_at >= _UNAVAILABLE_LOG_INTERVAL_S:
        _unavailable_logged_at = now
        logger.warning("ADS-B snapshot: the privacy policy is unreadable; no node's fixes are archived meanwhile")


def _pop_if_held(d: dict, key, value) -> None:
    """Pop d[key] only while it still holds value, not one a writer has put there since."""
    if d.get(key) is value:
        d.pop(key, None)


def _node_rows(ts_ms: int) -> list[dict]:
    """Rows from each reporter's own fixes, pruning the map's stale entries on the way."""
    fixes_by_hex = state.adsb_fixes_by_reporter
    candidates = []
    for hex_key, per_hex in _items(fixes_by_hex):
        for reporter, rec in _items(per_hex):
            # Exactly the fixes no snapshot at this time may write, either side.
            if not _is_fresh(ts_ms, rec.get("last_seen_ms")):
                _pop_if_held(per_hex, reporter, rec)
                continue
            world = rec.get("world")
            if world not in adsb_writer.WORLDS:
                continue
            if reporter is None and world != "sim":
                continue
            candidates.append((world, normalize_hex_key(hex_key), reporter, rec))
        if not per_hex:
            _pop_if_held(fixes_by_hex, hex_key, per_hex)

    verdict = _publishable({reporter for _, _, reporter, _ in candidates if reporter is not None})
    if verdict is None:
        _note_policy_unavailable()
        verdict = {}

    # The freshest fix per (world, hex), and in world "real" per reporter as well.
    best: dict[tuple, tuple[tuple, dict]] = {}
    for world, hexn, reporter, rec in candidates:
        if not hexn or (reporter is not None and not verdict.get(reporter)):
            continue
        row = _row(ts_ms, hexn, rec, world, "node", reporter)
        if row is None:
            continue
        key = (world, hexn, reporter) if world == "real" else (world, hexn)
        rank = (-rec["last_seen_ms"], reporter is not None, reporter or "")
        held = best.get(key)
        if held is None or rank < held[0]:
            best[key] = (rank, row)
    return [row for _, row in best.values()]


def max_published_offset_km() -> float:
    """The furthest any node's published position can sit from its true one.

    The donut's outer edge, plus the site radius a node joined to a site by
    proximity is published from beyond it, plus rounding.  Zero with the fuzz
    off, where the published position is the true one.
    """
    if not fuzz_enabled():
        return 0.0
    return node_fuzz_max_km() + node_fuzz_site_km() + _PUBLISHED_ROUNDING_KM


def clip_radius_km() -> float:
    """How far from a node's published position a polled fix is attributed to it."""
    return ADSB_NODE_RANGE_MARGIN_KM - max_published_offset_km() - _POLL_METRIC_SLACK_KM


def _attributable_members(ts_ms: int, polled) -> dict[tuple[int, int], list[tuple[str, float, float]]]:
    """Per polled cell, its settled nodes as (node id, published lat, published lon).

    The nodes are the ones the poller asks regions for, from the one selection
    both use (adsb_fallback.positioned_real_nodes).  This keeps those whose
    receiver is a usable place in a polled cell, as regions_for_nodes groups
    them, then applies the settle rule and the published-offset bound.
    """
    d_max = max_published_offset_km()
    seen: set[str] = set()
    out: dict[tuple[int, int], list[tuple[str, float, float]]] = {}
    for node_id, lat, lon in positioned_real_nodes():
        if not _is_place(lat, lon):
            continue
        cell = cell_of(lat, lon)
        if cell not in polled:
            continue
        seen.add(node_id)
        place = (cell, lat, lon)
        since = _member_since.get(node_id)
        if since is None or since[0] != place:
            _member_since[node_id] = (place, ts_ms)
            continue
        if ts_ms - since[1] < MEMBER_SETTLE_S * 1000.0:
            continue
        pub_lat, pub_lon = public_latlon(lat, lon, node_id)
        if not _is_place(pub_lat, pub_lon) or haversine_km(lat, lon, pub_lat, pub_lon) > d_max:
            continue
        out.setdefault(cell, []).append((node_id, float(pub_lat), float(pub_lon)))
    for node_id in [n for n in _member_since if n not in seen]:
        del _member_since[node_id]
    return out


def _clipping(cell_members: list[tuple[str, float, float]], lat: float, lon: float, radius: float) -> list[str]:
    """The ids of cell_members whose clip disc holds (lat, lon)."""
    dlat_max = (radius + _CLIP_LAT_SLACK_KM) / KM_PER_DEG_LAT
    return [
        node_id
        for node_id, plat, plon in cell_members
        if abs(plat - lat) <= dlat_max and haversine_km(plat, plon, lat, lon) <= radius
    ]


def _attribute(
    fixes: list[tuple[dict, tuple[int, int]]],
    members: dict[tuple[int, int], list[tuple[str, float, float]]],
    radius: float,
) -> list[tuple[dict, list[str]]]:
    """One aircraft's fixes to write, each with the sorted ids it is attributed to.

    fixes are the aircraft's fresh, placed records, each with the cell that
    polled it.  Each one inside the clip disc of a node of its own cell is
    written, attributed to the nodes, of every cell among fixes, whose clip
    disc holds THAT fix.  Records of one fix (equal capture time and position)
    are written once.  Freshest first, then by position.
    """
    cells = list(dict.fromkeys(c for _, c in fixes))
    written: dict[tuple, tuple[dict, list[str]]] = {}
    for rec, cell in sorted(fixes, key=lambda fix: (-fix[0]["last_seen_ms"], fix[0]["lat"], fix[0]["lon"])):
        lat, lon = rec["lat"], rec["lon"]
        same_fix = (rec["last_seen_ms"], lat, lon)
        if same_fix in written:
            continue
        ids = _clipping(members[cell], lat, lon, radius)
        if not ids:
            continue
        for other in cells:
            if other != cell:
                ids += _clipping(members[other], lat, lon, radius)
        written[same_fix] = (rec, sorted(ids))
    return list(written.values())


def _aggregator_rows(ts_ms: int) -> list[dict]:
    """Rows from the polled ADSBHub answers, clipped to the nodes they are attributed to."""
    # Replaced whole each cycle and never edited: this is one cycle's view.
    cells = state.adsb_fallback
    if not cells:
        _member_since.clear()
        return []
    members = _attributable_members(ts_ms, cells)
    radius = clip_radius_km()
    if not members or radius <= 0:
        return []
    by_hex: dict[str, list[tuple[dict, tuple[int, int]]]] = {}
    for cell, recs in list(cells.items()):
        if not members.get(cell):
            continue
        for hex_key, rec in list(recs.items()):
            if not _is_fresh(ts_ms, rec.get("last_seen_ms")) or not _is_place(rec.get("lat"), rec.get("lon")):
                continue
            hexn = normalize_hex_key(rec.get("hex") or hex_key)
            if hexn:
                by_hex.setdefault(hexn, []).append((rec, cell))
    rows = []
    for hexn, fixes in by_hex.items():
        for rec, ids in _attribute(fixes, members, radius):
            row = _row(ts_ms, hexn, rec, "real", "aggregator", ",".join(ids))
            if row is not None:
                rows.append(row)
    return rows


def snapshot_rows(ts_ms: int) -> list[dict]:
    """The archive rows for server time ts_ms (epoch ms)."""
    return _node_rows(ts_ms) + _aggregator_rows(ts_ms)


def take_snapshot(ts_ms: int | None = None) -> int:
    """Buffer one snapshot as a batch; returns its row count."""
    rows = snapshot_rows(int(time.time() * 1000) if ts_ms is None else ts_ms)
    if not rows:
        return 0
    with _buffer_lock:
        dropped = len(_buffer) == _buffer.maxlen
        _buffer.append(rows)
    if dropped:
        state.bump_counter("adsb_snapshot_dropped")
    return len(rows)


def _write_pending(write_ts: datetime | None, *, take_all: bool) -> str | None:
    """Write the pending batch, first taking the buffer if none is pending (or take_all).

    Caller holds _flush_lock.  Raises on a failed write, leaving the batch
    pending.
    """
    if take_all or not _pending_rows:
        # Moved under the buffer lock, so _rows_at_risk never finds a row in neither.
        with _buffer_lock:
            for batch in _buffer:
                _pending_rows.extend(batch)
            _buffer.clear()
    if not _pending_rows:
        return None
    key = write_adsb_parquet(
        rows=_pending_rows,
        base_dir=adsb_writer.ADSB_DIR,
        write_ts=write_ts or datetime.now(timezone.utc),
    )
    _pending_rows.clear()
    return key


def flush_adsb_snapshots(*, write_ts: datetime | None = None) -> str | None:
    """Write one raw part: the batch a failed flush left, else everything buffered.

    Returns its key, or None when there was nothing to write.  Raises on
    failure, keeping the batch for the next flush.
    """
    with _flush_lock:
        return _write_pending(write_ts, take_all=False)


def _rows_at_risk() -> int:
    """The rows pending and buffered, read without _flush_lock.

    A flush moves the buffer into the pending batch under _buffer_lock, so
    under it every row not yet written is in one or the other.
    """
    with _buffer_lock:
        return len(_pending_rows) + sum(len(batch) for batch in _buffer)


def final_flush_adsb_snapshots() -> None:
    """Write what is pending and buffered once more, best effort, at shutdown.

    Called by the lifespan once its tasks are cancelled and awaited, on the
    event loop, so it never waits: a flush that holds the lock then is one the
    task's executor has stopped waiting for.
    """
    if not _flush_lock.acquire(blocking=False):
        # The flush still writing may yet land its batch; nothing writes the buffer after it.
        logger.error(
            "ADS-B snapshot: a flush is still writing at shutdown; %d row(s) may not be written", _rows_at_risk()
        )
        return
    try:
        key = _write_pending(None, take_all=True)
        if key:
            logger.info("ADS-B snapshot: final flush wrote %s", key)
    except Exception:
        logger.exception("ADS-B snapshot: final flush failed; %d row(s) not written", _rows_at_risk())
    finally:
        _flush_lock.release()


async def adsb_snapshot_task() -> None:
    """The lifespan's task: snapshot every interval, flush every flush interval."""
    if not ADSB_SNAPSHOT_ENABLED:
        logger.info("ADS-B snapshots are off (ADSB_SNAPSHOT_ENABLED); nothing is archived")
        return
    # Cancelled, the executor waits for a flush in flight; what is left is
    # written by the lifespan (final_flush_adsb_snapshots).
    async with task_executor("adsb-snapshot") as run:
        last_flush = time.monotonic()
        while True:
            await asyncio.sleep(ADSB_SNAPSHOT_INTERVAL_S)
            try:
                take_snapshot()
                if time.monotonic() - last_flush >= ADSB_SNAPSHOT_FLUSH_INTERVAL_S:
                    last_flush = time.monotonic()
                    key = await run(flush_adsb_snapshots)
                    if key:
                        logger.debug("ADS-B snapshot flushed: %s", key)
                # A batch still pending is a write still failing.
                if not _pending_rows:
                    state.task_last_success[_TASK] = time.time()
            except Exception:
                state.bump_task_error(_TASK)
                logger.exception("adsb_snapshot_task iteration failed")
