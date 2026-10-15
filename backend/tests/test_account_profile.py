"""PATCH /api/auth/me: an account naming itself.

Through the app, with the anonymous-admin bypass off, as in
tests/test_session_routes.py: with it on, /api/auth/me answers the anonymous
admin whatever cookie is presented. The cookie goes by header, since it is
Secure and the test client speaks plain HTTP.
"""

import json
from collections import deque
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from unittest.mock import patch

import jwt
import pytest
from sqlalchemy import select

from config.constants import EVENT_LOG_MAX
from core import sessions, users
from core.auth import create_magic_link
from core.sessions import COOKIE_NAME, LEGACY_TOKEN_LIFETIME
from core.users import JWT_SECRET, User, async_session_maker
from routes import admin
from services import accounts
from tests.owner_helpers import real_sessions

ADA = "ada@example.com"
EMPTY = {"detail": "A name cannot be empty."}
TOO_LONG = {"detail": "A name is at most 64 characters."}
UNPRINTABLE = {"detail": "A name cannot contain control characters."}


@pytest.fixture(autouse=True)
def _cookies_are_read(monkeypatch, tmp_path):
    real_sessions(monkeypatch)
    # One test deletes an account, which is recorded: under tmp_path, not backend/data.
    monkeypatch.setattr(admin, "_events", deque(maxlen=EVENT_LOG_MAX))
    monkeypatch.setattr(admin, "_EVENTS_FILE", tmp_path / "events.json")


def _as(cookie: str, **headers: str) -> dict[str, str]:
    return {"Cookie": f"{COOKIE_NAME}={cookie}", **headers}


def _set_cookies(response) -> list[str]:
    """The value of every auth_token the response sets, in order."""
    found = []
    for header in response.headers.get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        if COOKIE_NAME in jar:
            found.append(jar[COOKIE_NAME].value)
    return found


async def _sign_in(client, email: str = ADA) -> str:
    token = await create_magic_link(email)
    r = client.post("/api/auth/magic-link/consume", json={"token": token})
    assert r.status_code == 200
    (cookie,) = _set_cookies(r)
    return cookie


async def _account(email: str = ADA) -> User:
    async with async_session_maker() as db:
        return (await db.scalars(select(User).where(User.email == email))).one()


def _rename(client, cookie: str | None, name, **headers: str):
    """PATCH with `name` as JSON, escaped to ASCII so that any code point, a
    lone surrogate included, reaches the server as the client sent it."""
    body = json.dumps({"name": name}).encode()
    cookies = _as(cookie) if cookie else {}
    return client.patch(
        "/api/auth/me", content=body, headers={"Content-Type": "application/json", **cookies, **headers}
    )


def _legacy_cookie(user: User) -> str:
    """A cookie as fastapi-users minted one (tests/test_sessions.py, _legacy_cookie)."""
    payload = {"sub": str(user.id), "aud": ["fastapi-users:auth"], "exp": datetime.now(UTC) + LEGACY_TOKEN_LIFETIME}
    return jwt.encode(payload, JWT_SECRET, "HS256")


class _AccessVerifier:
    def is_configured(self):
        return True

    async def identity(self, token):
        return "admin@example.com" if token else None


# ── Renaming ─────────────────────────────────────────────────────────────────


class TestRename:
    async def test_the_name_is_trimmed_stored_and_answered_as_me_answers(self, client):
        cookie = await _sign_in(client)
        assert (await _account()).name == "ada"

        r = _rename(client, cookie, "  Ada Lovelace \t")

        assert r.status_code == 200
        assert r.json()["name"] == "Ada Lovelace"
        assert r.json() == client.get("/api/auth/me", headers=_as(cookie)).json()
        assert (await _account()).name == "Ada Lovelace"

    async def test_the_name_outlives_the_session_it_was_set_in(self, client):
        cookie = await _sign_in(client)
        _rename(client, cookie, "Ada Lovelace")
        client.post("/api/auth/logout", headers=_as(cookie))

        again = await _sign_in(client)

        assert client.get("/api/auth/me", headers=_as(again)).json()["name"] == "Ada Lovelace"

    async def test_sixty_four_characters_are_accepted_once_trimmed(self, client):
        cookie = await _sign_in(client)

        r = _rename(client, cookie, "  " + "a" * 64 + "  ")

        assert r.status_code == 200
        assert (await _account()).name == "a" * 64

    @pytest.mark.parametrize("name", ["Ада Лавлейс", "李雷", "José Ñúñez", "🛰️ Ada", "Ada  Lovelace"])
    async def test_text_beyond_ascii_and_inner_spaces_are_a_name(self, client, name):
        cookie = await _sign_in(client)

        assert _rename(client, cookie, name).json()["name"] == name
        assert (await _account()).name == name

    async def test_another_account_keeps_its_name(self, client):
        cookie = await _sign_in(client)
        await _sign_in(client, "bob@example.com")

        _rename(client, cookie, "Ada Lovelace")

        assert (await _account("bob@example.com")).name == "bob"


# ── Refusals ─────────────────────────────────────────────────────────────────


class TestRefusals:
    @pytest.mark.parametrize(
        ("name", "answer"),
        [
            ("", EMPTY),
            ("   ", EMPTY),
            ("\t\n\r ", EMPTY),
            ("a" * 65, TOO_LONG),
            ("  " + "a" * 65 + "  ", TOO_LONG),
            ("Ada\x00", UNPRINTABLE),
            ("Ada\tLovelace", UNPRINTABLE),
            ("Ada\nLovelace", UNPRINTABLE),
            ("Ada\x1b[31m", UNPRINTABLE),
            ("Ada\x7f", UNPRINTABLE),
            ("Ada\x85Lovelace", UNPRINTABLE),
            # Format characters: unseen, and some reorder the text around them.
            ("Ada​Lovelace", UNPRINTABLE),
            ("Ada‍Lovelace", UNPRINTABLE),
            ("Ada‮ecalevoL", UNPRINTABLE),
            ("⁦Ada⁩", UNPRINTABLE),
            ("Ada﻿", UNPRINTABLE),
            ("Ada­Lovelace", UNPRINTABLE),
            # What breaks a line as a control would.
            ("Ada Lovelace", UNPRINTABLE),
            ("Ada Lovelace", UNPRINTABLE),
            # Decodes from JSON, and could not be stored.
            ("Ada\ud800", UNPRINTABLE),
        ],
    )
    async def test_is_422_with_the_reason_and_changes_nothing(self, client, name, answer):
        cookie = await _sign_in(client)

        r = _rename(client, cookie, name)

        assert r.status_code == 422
        assert r.json() == answer
        assert (await _account()).name == "ada"

    @pytest.mark.parametrize(
        "raw",
        [
            b'{"name": null}',
            b'{"name": 5}',
            b'{"name": ["Ada"]}',
            b"{}",
            b'{"name": "Ada", "email": "eve@example.com"}',
            b'"Ada"',
            b"not json",
            b"",
        ],
    )
    async def test_a_body_that_is_not_one_name_is_422_and_changes_nothing(self, client, raw):
        cookie = await _sign_in(client)

        r = client.patch("/api/auth/me", content=raw, headers={"Content-Type": "application/json", **_as(cookie)})

        assert r.status_code == 422
        assert (await _account()).name == "ada"


# ── Who may rename ───────────────────────────────────────────────────────────


class TestWhoMayRename:
    @pytest.mark.parametrize("name", ["Ada Lovelace", ""])
    def test_a_signed_out_caller_is_401_whatever_it_sends(self, client, name):
        r = _rename(client, None, name)

        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}

    async def test_a_deactivated_account_is_refused_and_keeps_its_name(self, client):
        cookie = await _sign_in(client)
        await sessions.deactivate_account((await _account()).id)

        r = _rename(client, cookie, "Ada Lovelace")

        assert r.status_code == 401
        assert (await _account()).name == "ada"

    def test_an_administrator_with_no_cookie_session_has_no_account_to_rename(self, client, monkeypatch):
        with patch("core.users.access_identity", _AccessVerifier()):
            access = _rename(client, None, "Admin", **{"Cf-Access-Jwt-Assertion": "a-token"})
        monkeypatch.setattr(users, "AUTH_BYPASS", True)
        anonymous = _rename(client, None, "Admin")

        assert access.status_code == anonymous.status_code == 401

    async def test_a_legacy_cookie_is_exchanged_for_a_session_and_renames(self, client):
        legacy = _legacy_cookie(await users.get_or_create_magic_link_user(ADA))

        r = _rename(client, legacy, "Ada Lovelace")

        assert r.status_code == 200
        (fresh,) = _set_cookies(r)
        assert fresh and "." not in fresh
        assert (await _account()).name == "Ada Lovelace"

    async def test_a_refusal_still_hands_over_the_session_a_legacy_cookie_was_exchanged_for(self, client):
        legacy = _legacy_cookie(await users.get_or_create_magic_link_user(ADA))

        r = _rename(client, legacy, "")

        assert r.status_code == 422
        (fresh,) = _set_cookies(r)
        assert client.get("/api/auth/me", headers=_as(fresh)).status_code == 200

    async def test_the_write_refuses_an_account_deactivated_since_its_cookie_was_read(self):
        """What the route's 401 rests on when the deactivation lands between
        the cookie being resolved and the name being written."""
        ada = await users.get_or_create_magic_link_user(ADA)
        await sessions.deactivate_account(ada.id)

        assert await accounts.rename(ada.id, "Ada Lovelace") is None
        assert (await _account()).name == "ada"

    async def test_the_write_finds_nothing_for_an_account_that_is_gone(self):
        ada = await users.get_or_create_magic_link_user(ADA)
        assert await accounts.delete_account(ada.id, by=ADA) == 0

        assert await accounts.rename(ada.id, "Ada Lovelace") is None

    async def test_an_old_cookie_does_not_rename_a_refreshed_session_past_its_end(self, client):
        """A session past its idle end is signed out like any other route's."""
        cookie = await _sign_in(client)
        later = datetime.now(UTC) + sessions.IDLE_LIFETIME + timedelta(minutes=1)

        with patch("core.sessions._utcnow", return_value=later):
            r = _rename(client, cookie, "Ada Lovelace")

        assert r.status_code == 401
        assert (await _account()).name == "ada"
