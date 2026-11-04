"""Node performance files as published, held in memory for the routes.

The daily job leaves ``performance_<id>.json`` in ``DATA_DIR``, plus
``performance_<id>.owner.json`` for a node whose owner holds only part of the window.
``reload`` reads them into two maps, one per kind, and readers ask the maps, never
the disk.

What a read does to a map depends on what it says about the file.  A file gone from
the folder is out, and no folder means nothing is published.  A file that reads but
can't be used is out too, with a warning.  A failed read says nothing about the file,
so the last value stands: a passing fault must not blank a published rank.

Every reader shares the same documents, so nobody changes one.  The job's child may
import this module, so it imports neither ``main`` nor ``core.state``.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[1] / "coverage_data"

_SCHEMA = 1
# Whole names only: a dotfile or a ``.tmp`` left by a writer doesn't match.
_FILE_NAME_RE = re.compile(r"performance_(.+?)(\.owner)?\.json")


class _Files(NamedTuple):
    public: dict[str, dict]
    owner: dict[str, dict]


# Both maps sit behind one name, so a swap is one assignment and no reader holds a mix.
_files = _Files({}, {})


def _usable(doc: object, node_id: str) -> bool:
    if not isinstance(doc, dict) or doc.get("schema") != _SCHEMA or doc.get("node_id") != node_id:
        return False
    window = doc.get("window")
    return "rank_status" in doc and isinstance(window, list) and [type(day) for day in window] == [str, str]


def _refuse_constant(name: str) -> None:
    raise ValueError(f"non-finite constant {name}")


def _read(path: Path, node_id: str) -> dict | None:
    """One file's document, or None when it reads but can't be used.

    OSError means it couldn't be read, which says nothing about it; a file removed
    since the listing raises FileNotFoundError.
    """
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f, parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as exc:
        logger.warning("performance file %s left out: %s", path.name, type(exc).__name__)
        return None
    if not _usable(doc, node_id):
        logger.warning("performance file %s left out: wrong shape or node id", path.name)
        return None
    return doc


def reload() -> None:
    """Read ``DATA_DIR`` again and swap in both maps at once."""
    global _files
    try:
        # Not Path.glob, which takes a listing that failed for an empty folder.
        names = os.listdir(DATA_DIR)
    except FileNotFoundError:
        names = []
    except OSError as exc:
        logger.warning("performance files not listed, keeping the ones held: %s", exc)
        return
    fresh = _Files({}, {})
    for name in names:
        found = _FILE_NAME_RE.fullmatch(name)
        if found is None:
            continue
        node_id = found[1]
        target, held = (fresh.owner, _files.owner) if found[2] else (fresh.public, _files.public)
        try:
            doc = _read(DATA_DIR / name, node_id)
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("performance file not read, keeping the last value: %s", exc)
            doc = held.get(node_id)
        if doc is not None:
            target[node_id] = doc
    _files = fresh


def get(node_id: str) -> dict | None:
    """A node's public file, or None."""
    return _files.public.get(node_id)


def get_owner(node_id: str) -> dict | None:
    """A node's owner file, or None."""
    return _files.owner.get(node_id)


def all() -> dict[str, dict]:
    """Every public file by node id, in a new dict.  The name hides the builtin in this module."""
    return dict(_files.public)


def has_claims_window() -> bool:
    """Whether any public file has a null ``rank_status`` and reads claims.

    False settles it, and saves ``performance_views.ranks`` the analytics snapshot.  True
    doesn't: public as against an owner's file, as nothing here knows which nodes are
    private or on the board, so ``ranks`` asks again of the files that can take a place.
    """
    return any(doc["rank_status"] is None and doc.get("numerator") == "claims" for doc in _files.public.values())
