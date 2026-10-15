"""The signed-in account itself: its display name, and deleting it.

Both answer the account the auth_token cookie signs in, through the check the
session routes in routes/auth.py use: an Access identity or the anonymous
admin has no account here to rename or delete. services/accounts.py does the
work.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from core import sessions
from core.users import User, UserSession, normalise_email, user_to_dict
from routes.auth import _session_user, _signed_in_session
from services import accounts

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: The account and the session asking, as a dependency, so a signed-out
#: caller is 401 before its body is looked at.
SignedIn = Annotated[tuple[User, UserSession], Depends(_signed_in_session)]


class NameEdit(BaseModel):
    # Strict and closed, as the node edit is (routes/owner.py, NodeEdit).
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str


class AccountDeletion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    #: The account's address, typed out to confirm.
    email: str


@router.patch("/me")
async def rename_me(signed_in: SignedIn, body: NameEdit, response: Response):
    """Change the caller's display name, and answer with the user as GET /api/auth/me does.

    The name is trimmed. A refused one is 422 with the reason as the detail,
    for the console to show as it is. Refusals are answered rather than
    raised, so that a cookie exchanged on the way in still reaches the browser.
    """
    user, _ = signed_in
    try:
        renamed = await accounts.rename(user.id, body.name)
    except accounts.NameRefused as exc:
        response.status_code = 422
        return {"detail": str(exc)}
    if renamed is None:
        # Deactivated or deleted since its cookie was resolved.
        response.status_code = 401
        return {"detail": "Not authenticated"}
    # A cookie session is never the anonymous admin, which is what /me's flag
    # tells apart.
    return {**_session_user(user_to_dict(renamed)), "auth_enabled": True}


@router.post("/me/delete")
async def delete_me(signed_in: SignedIn, body: AccountDeletion, response: Response):
    """Delete the caller's account once they have typed its address, and sign them out.

    Every node it owns is handed back as its own release would, every session
    ends and the row goes (services/accounts.py, delete_account). The answer
    says how many nodes were handed back.
    """
    user, _ = signed_in
    if normalise_email(body.email) != normalise_email(user.email):
        response.status_code = 400
        return {"detail": "That is not this account's address"}
    try:
        released = await accounts.delete_account(user.id, by=accounts.SELF)
    except sessions.AccountHoldsNodes:
        # Claims kept binding nodes to it while it was being deleted. It is
        # still there, still signed in, and holds only those.
        response.status_code = 409
        return {"detail": "A node was claimed for this account while it was being deleted. Try again."}
    sessions.clear_session_cookie(response)
    # None when another request deleted it first, which is what was asked.
    return {"ok": True, "released": released or 0}
