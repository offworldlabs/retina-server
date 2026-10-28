"""Public data archive API.

Unauthenticated, and it hands out whole detection files: per-detection delay,
Doppler and SNR plus the node's rx/tx geometry, for one node at a time.  That
makes it the most literal reading of "publish this node's detections" the
server has, so a node whose owner registered it private is not listed here and
its keys do not download.

No key, parameter or body it hands out names a node's private id; keys go out
and come back under the node's published identity (``node_ref=``).  That
handling lives in services/archive_access.py, which an owner's own archive
reads through as well, with a scope of their nodes in place of this one.
"""

from fastapi import APIRouter, Query

from services.archive_access import key_node_id as _key_node_id  # noqa: F401  (tests/test_publication.py)
from services.archive_access import list_files, public_scope, read_file
from services.storage import list_archived_files, read_archived_file

router = APIRouter(tags=["archive"])


@router.get("/api/data/archive")
async def list_archive(
    date: str = Query(None, description="Date prefix, e.g. 2025/06/21"),
    node_ref: str = Query(None, description="Filter by node_ref"),
    limit: int = Query(50, ge=1, le=500, description="Page size"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    """One page of the archive's detection files, newest first.

    Each file is `{key, size_bytes, modified, first_frame_ms,
    node_published_lat, node_published_lon}`. `key` names the node by its
    node_ref, and `modified` is when the file was last written (ISO 8601,
    UTC). `first_frame_ms` is the time of the earliest detection frame in the
    file, in epoch milliseconds, read from the file itself; it is null for a
    legacy JSON file or a file that does not record it.
    `node_published_lat`/`node_published_lon` are the node's published
    position as /api/radar/nodes gives it, null where that list does not
    place the node.
    """
    # The store is passed by this module's names rather than left to the
    # defaults: they are what tests/test_publication.py replaces it through.
    return await list_files(
        public_scope(), date=date, node_ref=node_ref, limit=limit, offset=offset, lister=list_archived_files
    )


@router.get("/api/data/archive/{key:path}")
async def download_archive_file(key: str):
    # The policy is read before the key is resolved, and both before the read:
    # a private node's key gets the same answer as a key that does not exist,
    # so the endpoint cannot be used to enumerate the private fleet.
    return read_file(public_scope(), key, reader=read_archived_file)
