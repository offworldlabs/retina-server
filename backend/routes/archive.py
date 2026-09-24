"""Public data archive API.

Unauthenticated, and it hands out whole detection files: per-detection delay,
Doppler and SNR plus the node's rx/tx geometry, for one node at a time.  That
makes it the most literal reading of "publish this node's detections" the
server has, so a node whose owner registered it private is not listed here and
its keys do not download.

The store is Hive-partitioned on the private id
(``year=/month=/day=/node_id=/part-*.parquet``), so the id is in every key on
disk.  The route publishes each key with that segment rewritten to the node's
published identity (``node_ref=``), resolves it back on the way in, and renames
the downloaded body's ``node_id`` the same way: no key, parameter or body it
hands out names the private id.  See ``_published_key`` and ``_stored_key``.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Query

from services.node_refs import id_for_identity, public_identity
from services.publication import private_node_ids
from services.storage import list_archived_files, read_archived_file

router = APIRouter(tags=["archive"])

# The node segment's Hive name on disk, and the one it is published under.
_STORED = "node_id="
_PUBLISHED = "node_ref="


def _key_node_id(key: str) -> str:
    """The node id a stored key is partitioned under, or "" if it has none.

    Reads the second-to-last path segment, which is where both the Hive layout
    (``node_id=NNN``) and the legacy one (a bare directory name) put it, and
    strips the Hive prefix — the same derivation
    services/storage.list_archived_files uses to answer its own ``node_id``
    filter.  A key shaped like neither yields "" and is treated as public: it
    predates the partitioning and names no node to withhold.
    """
    parts = key.strip("/").split("/")
    if len(parts) < 2:
        return ""
    return parts[-2].split("=", 1)[-1]


def _published_key(key: str) -> str | None:
    """A stored key as the listing publishes it, or None if its node has no
    published identity.  A key with no node segment is published as it is."""
    parts = key.strip("/").split("/")
    if len(parts) < 2:
        return key
    hive = parts[-2].startswith(_STORED)
    ref = public_identity(parts[-2].removeprefix(_STORED))
    if ref is None:
        return None
    parts[-2] = f"{_PUBLISHED}{ref}" if hive else ref
    return "/".join(parts)


def _stored_key(key: str) -> tuple[str, str] | None:
    """The stored key behind a published one, and the node id it names ("" for
    a key with no node segment); None if it names no published node.

    Only the published spellings resolve: a ``node_id=`` segment, or a bare
    one holding a private id, is a key this route never handed out.
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


@router.get("/api/data/archive")
async def list_archive(
    date: str = Query(None, description="Date prefix, e.g. 2025/06/21"),
    node_ref: str = Query(None, description="Filter by node_ref"),
    limit: int = Query(50, ge=1, le=500, description="Page size"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    private = private_node_ids()
    node_id = None
    if node_ref:
        # An unknown ref and a private node's answer the same, so the filter
        # cannot confirm that a private node exists.
        node_id = id_for_identity(node_ref)
        if node_id is None or node_id in private:
            return {"files": [], "count": 0, "total": 0}
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: list_archived_files(date_prefix=date, node_id=node_id, limit=limit, offset=offset)
    )
    # Filtered after paging rather than before it, which shortens a page
    # instead of resampling the store: list_archived_files walks the tree in
    # date order with an early exit, and pushing the predicate into it would
    # mean either teaching storage.py about publication or re-listing until a
    # page fills.  `count` follows what is actually returned; `total` is left
    # as the scan reported, the same approximation it already is for the
    # truncated no-date path.
    files = []
    for f in result.get("files", []):
        key = f.get("key", "")
        if _key_node_id(key) in private:
            continue
        published = _published_key(key)
        if published is not None:
            files.append({**f, "key": published})
    return {**result, "files": files, "count": len(files)}


@router.get("/api/data/archive/{key:path}")
async def download_archive_file(key: str):
    # The policy is read before the key is resolved, and both before the read:
    # a private node's key gets the same answer as a key that does not exist,
    # so the endpoint cannot be used to enumerate the private fleet.
    private = private_node_ids()
    stored = _stored_key(key)
    if stored is None or stored[1] in private:
        raise HTTPException(status_code=404, detail="Archive file not found")
    data = read_archived_file(stored[0])
    if data is None:
        raise HTTPException(status_code=404, detail="Archive file not found")
    if isinstance(data, dict) and "node_id" in data:
        data = dict(data)
        body_id = data.pop("node_id")
        # Named from the key where it has a node, which is already resolved: a
        # zero-row file's own node_id is "".  A key with no node segment was
        # never checked above, so the node its body names is checked here.
        node_id = stored[1] or body_id
        if node_id in private:
            raise HTTPException(status_code=404, detail="Archive file not found")
        data["node_ref"] = public_identity(node_id)
    return data
