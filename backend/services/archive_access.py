"""Which detection archive files a caller may list and download, and under what names.

The store is Hive-partitioned on the private id
(``year=/month=/day=/node_id=/part-*.parquet``), so the id is in every key on
disk.  What goes out names the node by its published identity instead
(``node_ref=``): a listed key has that segment rewritten, a key that comes back
is resolved through it, and a downloaded body's ``node_id`` is renamed the same
way, so no key, parameter or body handed out names the private id.  See
``_published_key`` and ``_stored_key``.

Two archives read the store through this module.  The public one
(routes/archive.py) covers every node but the private ones; an owner's covers
the caller's own nodes, private ones included, and nothing else, and of each
only what was recorded since it became theirs.  An ``ArchiveScope`` says
which, and ``list_files`` and ``read_file`` apply it, so the two share the key
handling and the not-found answer rather than keeping a copy each.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Container, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from services.node_refs import id_for_identity, owner_identity, public_identity
from services.publication import private_node_ids
from services.storage import list_archived_files, read_archived_file

# The node segment's Hive name on disk, and the one it is published under.
_STORED = "node_id="
_PUBLISHED = "node_ref="

# What a `date` may be: a year, a month or a day, as the store's directories
# name them (services/storage.list_archived_files).  The store joins it onto
# its root as a path, so anything else is at best a listing of nothing and at
# worst a walk outside the archive.
_DATE = re.compile(r"[0-9]{4}(/[0-9]{2}){0,2}")

# A flush's file name, part-HHMMSS-<batch>.parquet (services/parquet_io.py):
# the second it was written, on the day its partition names.
_WRITTEN = re.compile(r"part-([0-9]{2})([0-9]{2})([0-9]{2})-")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class ArchiveScope:
    """The nodes whose files one caller may see, and what they go out named as.

    ``nodes`` is a finite set to keep to, or None for every node; ``withheld``
    is struck from either.  ``identity`` names a node in keys and bodies.
    ``since`` holds, per node, the start of the caller's time with it: a file
    written before then is out of scope, and so is a frame taken before then
    in a file written after.  A node it does not name has no such bound.
    """

    nodes: frozenset[str] | None
    withheld: Container[str]
    identity: Callable[[str], str | None]
    since: Mapping[str, datetime] = field(default_factory=dict)

    def admits(self, node_id: str) -> bool:
        """Whether a node's files are in scope.  "" is a key with no node segment."""
        if self.nodes is not None and node_id not in self.nodes:
            return False
        return node_id not in self.withheld

    def admits_key(self, key: str) -> bool:
        """Whether a stored key is in scope: its node is, and the file was
        written no earlier than the node's ``since``.  A key that does not say
        when it was written is out wherever there is a ``since`` to meet."""
        node_id = key_node_id(key)
        if not self.admits(node_id):
            return False
        start = self.since.get(node_id)
        if start is None:
            return True
        written = written_from(key)
        return written is not None and written >= start


def public_scope() -> ArchiveScope:
    """Every node but the ones their owners registered private.

    Reads the policy, so it raises PublicationUnavailable (main.py answers 503)
    until one has been read.
    """
    return ArchiveScope(nodes=None, withheld=private_node_ids(), identity=public_identity)


def owner_scope(tenures: Mapping[str, datetime]) -> ArchiveScope:
    """The caller's own nodes, private ones included, and no other, each from
    when it became theirs: ``tenures`` is core/auth.get_user_tenures's answer.

    Named through ``owner_identity``: a node of theirs with no handle is left
    out of a listing without the public boundary's log line.
    """
    return ArchiveScope(nodes=frozenset(tenures), withheld=frozenset(), identity=owner_identity, since=dict(tenures))


def key_node_id(key: str) -> str:
    """The node id a stored key is partitioned under, or "" if it has none.

    Reads the second-to-last path segment, which is where both the Hive layout
    (``node_id=NNN``) and the legacy one (a bare directory name) put it, and
    strips the Hive prefix — the same derivation
    services/storage.list_archived_files uses to answer its own ``node_id``
    filter.  A key shaped like neither yields "": it predates the partitioning
    and names no node.
    """
    parts = key.strip("/").split("/")
    if len(parts) < 2:
        return ""
    return parts[-2].split("=", 1)[-1]


def written_from(key: str) -> datetime | None:
    """The earliest the file behind a stored key can have been written, or None
    if the key does not say.

    Both layouts put the UTC day the file was written in the three segments
    above the node's (``year=YYYY/month=MM/day=DD`` or ``YYYY/MM/DD``).  A
    flush's file adds the second (``part-HHMMSS-...``); any other file name
    is taken to have been written at the day's first instant.
    """
    parts = key.strip("/").split("/")
    if len(parts) < 5:
        return None
    try:
        year, month, day = (int(part.split("=", 1)[-1]) for part in parts[-5:-2])
        written = datetime(year, month, day, tzinfo=UTC)
        if match := _WRITTEN.match(parts[-1]):
            hour, minute, second = (int(group) for group in match.groups())
            written = written.replace(hour=hour, minute=minute, second=second)
    except ValueError:
        return None
    return written


def _frames_since(data: dict, start: datetime) -> dict:
    """``data`` with only the frames taken at or after ``start``.

    A flush writes what the node sent since the last one, so the first file
    written after ``start`` can still hold frames from before it.  Frames are
    judged by their own ``timestamp``, as the SNR trend is; one without a
    number there cannot be shown to be after ``start`` and goes too.
    """
    detections = data.get("detections")
    if not isinstance(detections, list):
        return data
    # Rounded up: a frame in the millisecond before `start` is not after it.
    start_ms = -((_EPOCH - start) // timedelta(milliseconds=1))
    kept = [
        frame
        for frame in detections
        if isinstance(frame, dict)
        and isinstance(frame.get("timestamp"), int | float)
        and not isinstance(frame.get("timestamp"), bool)
        and frame["timestamp"] >= start_ms
    ]
    return {**data, "detections": kept, "count": len(kept)}


def _published_key(key: str, identity: Callable[[str], str | None]) -> str | None:
    """A stored key as a listing publishes it, or None if its node has no
    published identity.  A key with no node segment is published as it is."""
    parts = key.strip("/").split("/")
    if len(parts) < 2:
        return key
    hive = parts[-2].startswith(_STORED)
    ref = identity(parts[-2].removeprefix(_STORED))
    if ref is None:
        return None
    parts[-2] = f"{_PUBLISHED}{ref}" if hive else ref
    return "/".join(parts)


def _stored_key(key: str) -> tuple[str, str] | None:
    """The stored key behind a published one, and the node id it names ("" for
    a key with no node segment); None if it names no published node.

    Only the published spellings resolve: a ``node_id=`` segment, or a bare
    one holding a private id, is a key no listing handed out.
    """
    parts = key.strip("/").split("/")
    if len(parts) < 2:
        return key, ""
    hive = parts[-2].startswith(_PUBLISHED)
    if "=" in parts[-2] and not hive:
        return None
    node_id = id_for_identity(parts[-2].removeprefix(_PUBLISHED))
    if node_id is None:
        return None
    parts[-2] = f"{_STORED}{node_id}" if hive else node_id
    return "/".join(parts), node_id


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Archive file not found")


async def list_files(
    scope: ArchiveScope,
    *,
    date: str | None = None,
    node_ref: str | None = None,
    limit: int = 50,
    offset: int = 0,
    lister: Callable[..., dict] = list_archived_files,
) -> dict:
    """One page of the scope's files under their published keys.

    ``date``, ``limit`` and ``offset`` mean what they mean to
    list_archived_files, and the answer has its shape: ``files``, ``count``,
    ``total``, and ``truncated`` on a scan with no date.  ``node_ref`` narrows
    it to one node.  ``lister`` is the store.

    A ``date`` that is not ``YYYY``, ``YYYY/MM`` or ``YYYY/MM/DD`` is 422
    before anything is read: the store takes it as a path under its root.
    """
    if date and not _DATE.fullmatch(date):
        raise HTTPException(status_code=422, detail="date must be YYYY, YYYY/MM or YYYY/MM/DD")
    empty = {"files": [], "count": 0, "total": 0}
    node_id = None
    if node_ref:
        # An unknown ref and one outside the scope answer the same, so the
        # filter cannot confirm that a node the scope leaves out exists.
        node_id = id_for_identity(node_ref)
        if node_id is None or not scope.admits(node_id):
            return empty
    if scope.nodes is not None and not scope.nodes:
        return empty
    # The tenure goes into the scan as the set does, so an owner's pages fill
    # from files that are theirs to see.  Asked for only where there is one,
    # which leaves the public archive's call to the store as it was.
    bounded = {"admit": scope.admits_key} if scope.since else {}
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: lister(date_prefix=date, node_id=node_id, node_ids=scope.nodes, limit=limit, offset=offset, **bounded),
    )
    # A finite scope is kept to by the scan itself, so its pages fill from its
    # own nodes' files.  The public one, every node bar the private ones, is
    # filtered here, after paging, which shortens a page instead of resampling
    # the store: list_archived_files walks the tree in date order with an early
    # exit, and pushing that predicate into it would mean either teaching
    # storage.py about publication or re-listing until a page fills.  `count`
    # follows what is actually returned; `total` is left as the scan reported,
    # the same approximation it already is for the truncated no-date path.
    files = []
    for f in result.get("files", []):
        key = f.get("key", "")
        if not scope.admits_key(key):
            continue
        published = _published_key(key, scope.identity)
        if published is not None:
            files.append({**f, "key": published})
    return {**result, "files": files, "count": len(files)}


def read_file(
    scope: ArchiveScope,
    key: str,
    *,
    reader: Callable[[str], dict | None] = read_archived_file,
) -> dict:
    """The file behind a published key, with its ``node_id`` renamed to ``node_ref``.

    Raises the archive's 404 for a key outside the scope exactly as for one
    that does not exist, decided before the store is read, so the endpoint
    cannot be used to enumerate what the scope leaves out.  A file written
    since the node's ``since`` is given without the frames it holds from
    before then.  ``reader`` is the store.
    """
    stored = _stored_key(key)
    if stored is None or not scope.admits_key(stored[0]):
        raise _not_found()
    data = reader(stored[0])
    if data is None:
        raise _not_found()
    node_id = stored[1]
    if isinstance(data, dict) and "node_id" in data:
        data = dict(data)
        body_id = data.pop("node_id")
        # Named from the key where it has a node, which is already resolved: a
        # zero-row file's own node_id is "".  A key with no node segment was
        # admitted on "" above, so the node its body names is checked here.
        node_id = stored[1] or body_id
        if not scope.admits(node_id):
            raise _not_found()
        data["node_ref"] = scope.identity(node_id)
    start = scope.since.get(node_id)
    if start is not None and isinstance(data, dict):
        data = _frames_since(data, start)
    return data
