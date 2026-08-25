"""Runtime tower-config read/write endpoints.

The payload is tower config, but these are operator surfaces rather than part
of the tower search: they answer on every monolith vhost, and the only callers
are the smoke tests, the e2e suite and the runbook.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from core.runtime_config import write_runtime_file
from core.users import require_admin
from services.tower_ranking import (
    _CONFIG_PATH,
    apply_config,
    reload_config,
    validate_config,
)

router = APIRouter()


@router.get("/api/config")
async def get_config():
    with open(_CONFIG_PATH) as f:
        return json.load(f)


@router.put("/api/config")
async def update_config(body: dict, _admin=Depends(require_admin)):
    # Sanity check: config should be a reasonable size
    raw = json.dumps(body)
    if len(raw) > 1_000_000:
        raise HTTPException(status_code=413, detail="Config too large (max 1 MB)")

    # Validate it, prove it applies, and only then write it. _CONFIG_PATH lives
    # in a persistent docker volume and reload_config() runs at import, so a
    # config that reaches disk without applying cleanly outlives both a restart
    # and a redeploy, recoverable only by hand inside the volume. Writing last
    # means the file only ever holds a config the running process has accepted,
    # so there is no rollback to get wrong.
    error = validate_config(body)
    if error:
        raise HTTPException(status_code=400, detail=f"Invalid config: {error}")

    try:
        apply_config(body)
    except Exception as exc:
        # validate_config has a gap. apply_config is all-or-nothing and the file
        # is still untouched, so the running config is unchanged.
        logging.exception("Config passed validation but would not apply")
        raise HTTPException(status_code=400, detail=f"Config could not be applied: {exc}") from exc

    try:
        write_runtime_file(_CONFIG_PATH, json.dumps(body, indent=2))
    except OSError as exc:
        # The process has already taken this config but the file has not. Put
        # the two back in step by re-reading whatever is actually on disk.
        logging.exception("Config applied but could not be written")
        reload_config()
        raise HTTPException(status_code=500, detail=f"Config could not be written: {exc}") from exc
    return {"status": "updated"}
