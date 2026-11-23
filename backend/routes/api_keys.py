"""An account's API keys: its access status, and making, listing and revoking its keys.

For the key page of a signed-in account. `access` says whether the account may
use the keyed data API and under which limits; the key routes make a key, list
the account's keys and revoke one. The full key is in the response that makes
it and nowhere else: only its hash is stored (services/api_keys.py).

An administrator is always entitled and has no account row, so has no keys to
make or list; their access answers as the full, unlimited one.
"""

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.users import get_current_user
from services.api_keys import create_key, key_to_dict, list_keys, revoke_key
from services.entitlements import access_status, admin_access, effective_access

router = APIRouter(prefix="/api/auth/me", tags=["auth"])

SignedIn = Annotated[dict, Depends(get_current_user)]


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


def _is_admin(user: dict) -> bool:
    return user.get("role") == "admin"


@router.get("/access")
async def my_access(user: SignedIn):
    """Whether the caller may use the keyed data API, and under which limits."""
    if _is_admin(user):
        limits = admin_access().limits
        assert limits is not None
        return {
            "entitled": True,
            "kind": "admin",
            "reason": None,
            "limits": asdict(limits),
            "contributor": None,
            "grant_ends_at": None,
        }
    return (await access_status(user["id"])).to_dict()


@router.get("/keys")
async def my_keys(user: SignedIn):
    """The caller's keys, the latest first and the revoked ones included, never a secret."""
    if _is_admin(user):
        return []
    return [key_to_dict(key) for key in await list_keys(user["id"])]


@router.post("/keys", status_code=201)
async def make_key(body: CreateKeyRequest, user: SignedIn):
    """Make a key for the caller. The full key is in this response only."""
    if _is_admin(user):
        raise HTTPException(403, "Keys belong to accounts. As an administrator you have full access without one.")
    if not (await effective_access(user["id"])).entitled:
        raise HTTPException(403, "API access is not active on this account, so it cannot create keys.")
    try:
        row, key = await create_key(user["id"], body.name)
    except ValueError:
        raise HTTPException(422, "Use a name of 1 to 100 characters.") from None
    except LookupError:
        raise HTTPException(404, "Account not found") from None
    return {**key_to_dict(row), "key": key}


@router.delete("/keys/{key_id}")
async def revoke_my_key(key_id: str, user: SignedIn):
    """Revoke one of the caller's keys. Another account's key answers as one that does not exist."""
    row = None if _is_admin(user) else await revoke_key(key_id, user_id=user["id"])
    if row is None:
        raise HTTPException(404, "Key not found")
    return key_to_dict(row)
