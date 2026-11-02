"""An account's API access from the admin API: read it, grant or revoke customer
access, and list or revoke its keys.

An administrator reads an account's access with every entitlement it ever held,
grants customer access with an optional end date (after which it lapses) and
revokes a grant. Revoking a grant disables the account's keys without deleting
them. An administrator also revokes any one key. Each change is logged in the
admin events. The account's own key page is routes/api_keys.py; the rules are
in services/entitlements.py and services/api_keys.py.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.api_access import Entitlement
from core.users import get_async_session, require_admin
from routes.admin import _account, log_event
from services.api_keys import key_to_dict, list_keys, revoke_key
from services.entitlements import access_status, grant_customer, list_entitlements, revoke_grant

router = APIRouter(prefix="/api/admin", tags=["admin"])


class GrantRequest(BaseModel):
    ends_at: datetime | None = None


def _iso(when: datetime | None) -> str | None:
    return None if when is None else when.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _entitlement(row: Entitlement) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "source": row.source,
        "starts_at": _iso(row.starts_at),
        "ends_at": _iso(row.ends_at),
        "lapsed_at": _iso(row.lapsed_at),
    }


@router.get("/users/{user_id}/access")
async def get_user_access(
    user_id: str,
    session: AsyncSession = Depends(get_async_session),
    _admin=Depends(require_admin),
):
    """The account's access now, and every entitlement it ever held, newest first."""
    account = await _account(session, user_id)
    return {
        "access": (await access_status(str(account.id))).to_dict(),
        "entitlements": [_entitlement(row) for row in await list_entitlements(str(account.id))],
    }


@router.post("/users/{user_id}/grants", status_code=201)
async def grant_user_access(
    user_id: str,
    body: GrantRequest,
    session: AsyncSession = Depends(get_async_session),
    admin=Depends(require_admin),
):
    """Grant the account customer access from now, until `ends_at` or with no end."""
    account = await _account(session, user_id)
    try:
        grant = await grant_customer(str(account.id), ends_at=body.ends_at)
    except ValueError:
        raise HTTPException(422, "The end must be later than now.") from None
    except LookupError:
        raise HTTPException(404, "User not found") from None
    ends_at = _iso(grant.ends_at)
    log_event(
        "user",
        f"Customer access granted to {account.email} " + (f"until {ends_at}" if ends_at else "with no end date"),
        "info",
        {"user_id": str(account.id), "entitlement_id": grant.id, "ends_at": ends_at, "by": admin["email"]},
    )
    return _entitlement(grant)


@router.delete("/grants/{entitlement_id}")
async def revoke_user_grant(entitlement_id: str, admin=Depends(require_admin)):
    """End a customer grant now. The account's keys stay, refused while it is not entitled."""
    grant = await revoke_grant(entitlement_id)
    if grant is None:
        raise HTTPException(404, "Grant not found")
    log_event(
        "user",
        f"Customer access {grant.id} revoked for account {grant.user_id}",
        "info",
        {"user_id": grant.user_id, "entitlement_id": grant.id, "by": admin["email"]},
    )
    return _entitlement(grant)


@router.get("/users/{user_id}/keys")
async def list_user_keys(
    user_id: str,
    session: AsyncSession = Depends(get_async_session),
    _admin=Depends(require_admin),
):
    """The account's keys, revoked ones included."""
    account = await _account(session, user_id)
    return [key_to_dict(row) for row in await list_keys(str(account.id))]


@router.delete("/keys/{key_id}")
async def revoke_any_key(key_id: str, admin=Depends(require_admin)):
    """Revoke any account's key; it is refused from this moment."""
    row = await revoke_key(key_id)
    if row is None:
        raise HTTPException(404, "Key not found")
    log_event(
        "user",
        f"API key {row.prefix} revoked for account {row.user_id}",
        "info",
        {"user_id": row.user_id, "key_id": row.id, "prefix": row.prefix, "by": admin["email"]},
    )
    return key_to_dict(row)
