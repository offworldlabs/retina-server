"""Act as a signed-in owner, or as nobody, in a route test.

The suite runs with the anonymous-admin bypass on (conftest.py), so a request
is answered as an administrator whatever cookie it carries and a 401 or a 404
for somebody else's node can never be seen. `real_sessions` turns that off,
`sign_in` gets a real cookie, and `as_user` puts it on the client or takes it
off.
"""

import asyncio
import sys

from sqlalchemy import select

from core.auth import create_magic_link
from core.users import User, async_session_maker, normalise_email

_SCOPES = ("core.", "routes.", "services.")


def real_sessions(monkeypatch) -> None:
    """Resolve requests from the session cookie alone.

    The flag is patched in core.users and in every loaded module that imported
    it by name (routes/streaming.py), since a copy bound at import is not
    reached by patching its source. A module loaded afterwards imports the
    patched value.
    """
    for name, module in list(sys.modules.items()):
        if name.startswith(_SCOPES) and "AUTH_BYPASS" in vars(module):
            monkeypatch.setattr(module, "AUTH_BYPASS", False)


def recheck_sessions_every(monkeypatch, seconds: float) -> None:
    """Check an open owner feed's session on the clock every `seconds`.

    routes/streaming.py's SESSION_RECHECK_S, and the same name in any other
    loaded module, as real_sessions does for AUTH_BYPASS: a second timer that
    checked the session would otherwise sit a test out at its own default,
    and whatever it did to the session would go unseen.
    """
    import routes.streaming  # noqa: F401  the one that must be patched

    for name, module in list(sys.modules.items()):
        if name.startswith(_SCOPES) and "SESSION_RECHECK_S" in vars(module):
            monkeypatch.setattr(module, "SESSION_RECHECK_S", seconds)


def sign_in(client, email: str) -> str:
    """Sign `email` in through the magic-link route and return the auth_token.

    The token comes from the response rather than the client's jar: the cookie
    is Secure and the test client speaks plain HTTP, so the jar drops it. The
    client is left as it was; `as_user` is what acts as the account.
    """
    token = asyncio.run(create_magic_link(email))
    assert token, f"no sign-in link issued for {email}"
    response = client.post("/api/auth/magic-link/consume", json={"token": token})
    assert response.status_code == 200, response.text
    return response.cookies["auth_token"]


def user_id_of(email: str) -> str:
    """The users.id of `email`, once it has signed in."""

    async def _lookup() -> str:
        async with async_session_maker() as session:
            user = (await session.execute(select(User).where(User.email == normalise_email(email)))).scalars().one()
            return str(user.id)

    return asyncio.run(_lookup())


def as_user(client, token: str | None) -> None:
    """Send `token` as the auth_token cookie from now on, or no cookie for None."""
    client.cookies.clear()
    if token is not None:
        client.cookies.set("auth_token", token)
