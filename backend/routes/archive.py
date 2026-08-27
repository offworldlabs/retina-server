"""Public data archive API.

Unauthenticated, and it hands out whole detection files: per-detection delay,
Doppler and SNR plus the node's rx/tx geometry, for one node at a time.  That
makes it the most literal reading of "publish this node's detections" the
server has, so a node whose owner registered it private is not listed here and
its keys do not download.  The keys are Hive-partitioned
(``year=/month=/day=/node_id=/part-*.parquet``), so the node id is in the key
itself and both checks read it from there — see ``_key_node_id``.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Query

from services.publication import private_node_ids
from services.storage import list_archived_files, read_archived_file

router = APIRouter()


def _key_node_id(key: str) -> str:
    """The node id a key is partitioned under, or "" if it has none.

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


@router.get("/api/data/archive")
async def list_archive(
    date: str = Query(None, description="Date prefix, e.g. 2025/06/21"),
    node_id: str = Query(None, description="Filter by node ID"),
    limit: int = Query(50, ge=1, le=500, description="Page size"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: list_archived_files(date_prefix=date, node_id=node_id, limit=limit, offset=offset)
    )
    private = private_node_ids()
    if not private:
        return result
    # Filtered after paging rather than before it, which shortens a page
    # instead of resampling the store: list_archived_files walks the tree in
    # date order with an early exit, and pushing the predicate into it would
    # mean either teaching storage.py about publication or re-listing until a
    # page fills.  `count` follows what is actually returned; `total` is left
    # as the scan reported, the same approximation it already is for the
    # truncated no-date path.
    files = [f for f in result.get("files", []) if _key_node_id(f.get("key", "")) not in private]
    return {**result, "files": files, "count": len(files)}


@router.get("/api/data/archive/{key:path}")
async def download_archive_file(key: str):
    # Before the read, not after: the point is not to avoid serving the bytes
    # but to give a private node's key the same answer as a key that does not
    # exist, so the endpoint cannot be used to enumerate the private fleet.
    if _key_node_id(key) in private_node_ids():
        raise HTTPException(status_code=404, detail="Archive file not found")
    data = read_archived_file(key)
    if data is None:
        raise HTTPException(status_code=404, detail="Archive file not found")
    return data
