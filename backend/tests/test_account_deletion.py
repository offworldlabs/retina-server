"""Deleting an account: POST /api/auth/me/delete, and services/accounts.py's
delete_account under it, which an administrator's route can call as well.

Through the app with the anonymous-admin bypass off, as in
tests/test_session_routes.py, and the cookie by header, since it is Secure and
the test client speaks plain HTTP.

The invariants each have a test here: no node is left naming a deleted
account, a claim racing the deletion included; a deletion that fails part way
leaves an account that still signs in and can be deleted again; and a sign-in
link minted before the deletion signs nobody in after it.
"""

import asyncio
import sqlite3
import time
from collections import deque
from datetime import UTC, datetime
from http.cookies import Morsel, SimpleCookie
from unittest.mock import patch

import jwt
import pytest
from sqlalchemy import delete, event, select

from config.constants import EVENT_LOG_MAX
from core import sessions, users
from core.auth import create_magic_link, peek_magic_link
from core.mail_outbox import MailOutbox
from core.nodes import Node, NodeClaim, NodeClaimChallenge, NodeEvent, PolledRadar
from core.rate_counters import RateCounter
from core.sessions import COOKIE_NAME, LEGACY_TOKEN_LIFETIME
from core.users import (
    JWT_SECRET,
    MagicLink,
    RetiredAddress,
    RetiredLegacyToken,
    User,
    UserSession,
    async_session_maker,
)
from routes import admin
from services import accounts, blah2_poller, claim_links, node_auth, node_claiming
from services.node_claim_store import put_challenge, set_claim_address
from services.node_claiming import ClaimOutcome, complete_claim
from services.polled_radars import create_polled_radar
from tests.claim_mail_helpers import configure_mail
from tests.owner_helpers import real_sessions
from tests.ownership_helpers import get_node_owner, own_registered_node, register_node_row
from tests.test_polled_radars import _args as _radar_args

ADA = "ada@example.com"
NEW = "ada.new@example.com"
BOB = "bob@example.com"
NODES = ("ret1a2b3c4d", "ret2b3c4d5e", "ret3a4b5c6d", "ret5e6f7a8b")
BOBS_NODE = "ret9f8e7d6c"
WRONG_ADDRESS = {"detail": "That is not this account's address"}
DEAD_SIGN_IN_LINK = {"detail": "That sign-in link is no longer valid"}


@pytest.fixture(autouse=True)
def _cookies_are_read(monkeypatch, tmp_path):
    real_sessions(monkeypatch)
    # This test's events alone, and saved under tmp_path rather than backend/data.
    monkeypatch.setattr(admin, "_events", deque(maxlen=EVENT_LOG_MAX))
    monkeypatch.setattr(admin, "_EVENTS_FILE", tmp_path / "events.json")


async def _empty_queue_and_counters() -> None:
    async with async_session_maker() as db:
        async with db.begin():
            await db.execute(delete(MailOutbox))
            await db.execute(delete(RateCounter))


@pytest.fixture
async def claim_mail(monkeypatch, tmp_path):
    """Claim mail that could go, queued as a node's nomination queues it, and
    the queue and the claim limits' counters emptied around the test, which
    conftest.py does not do: a node asking again here would otherwise be held
    to what an earlier test spent."""
    configure_mail(monkeypatch, tmp_path)
    await _empty_queue_and_counters()
    yield
    await _empty_queue_and_counters()


@pytest.fixture
def poller_wakes(monkeypatch):
    wakes = []
    monkeypatch.setattr(blah2_poller, "refresh", lambda: wakes.append(True))
    return wakes


def _as(cookie: str) -> dict[str, str]:
    return {"Cookie": f"{COOKIE_NAME}={cookie}"}


def _set_cookies(response) -> list[Morsel]:
    found = []
    for header in response.headers.get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        if COOKIE_NAME in jar:
            found.append(jar[COOKIE_NAME])
    return found


def _cleared(response) -> bool:
    """Whether the browser is left with no auth_token after this response."""
    set_cookies = _set_cookies(response)
    return bool(set_cookies) and set_cookies[-1]["max-age"] == "0" and set_cookies[-1].value == ""


async def _sign_in(client, email: str = ADA) -> str:
    token = await create_magic_link(email)
    r = client.post("/api/auth/magic-link/consume", json={"token": token})
    assert r.status_code == 200
    return _set_cookies(r)[0].value


def _me(client, cookie: str):
    return client.get("/api/auth/me", headers=_as(cookie))


def _delete(client, cookie: str, email: str = ADA):
    return client.post("/api/auth/me/delete", json={"email": email}, headers=_as(cookie))


async def _account(email: str = ADA) -> User | None:
    async with async_session_maker() as db:
        return (await db.scalars(select(User).where(User.email == email))).one_or_none()


async def _id_of(email: str = ADA) -> str:
    account = await _account(email)
    assert account is not None
    return str(account.id)


async def _owned_by(user_id: str) -> set[str]:
    async with async_session_maker() as db:
        return set(await db.scalars(select(NodeClaim.node_id).where(NodeClaim.user_id == user_id)))


async def _rows(model, **where) -> list:
    async with async_session_maker() as db:
        query = select(model)
        for column, value in where.items():
            query = query.where(getattr(model, column) == value)
        return list(await db.scalars(query))


async def _claim_link(email: str, node_id: str) -> str:
    """A live claim link for a registered node offering `email`, as
    tests/test_claim_routes.py sets one up."""
    await register_node_row(node_id)
    challenge = await claim_links.issue(email=email, node_id=node_id, now=time.time())
    assert challenge is not None
    async with async_session_maker() as db:
        async with db.begin():
            await set_claim_address(db, node_id, email)
            await put_challenge(db, node_id, email, challenge.handle, challenge.expires_at)
    return challenge.token


async def _mailed_claim_link(email: str, node_id: str) -> str:
    """`_claim_link`, with its mail queued as the node's nomination queues it:
    to the account's current address for one it retired."""
    node_ref = await register_node_row(node_id)
    challenge = await claim_links.issue(email=email, node_id=node_id, now=time.time())
    assert challenge is not None
    async with async_session_maker() as db:
        async with db.begin():
            await set_claim_address(db, node_id, email)
            await put_challenge(db, node_id, email, challenge.handle, challenge.expires_at)
            await claim_links.queue(db, challenge, email=email, node_id=node_id, node_ref=node_ref)
    return challenge.token


async def _moved(client) -> tuple[str, str]:
    """An account that signed in as ADA and has since moved to NEW, with ADA
    retired to it, signed in. Returns its cookie and its id."""
    async with async_session_maker() as db:
        async with db.begin():
            account = User(email=NEW, name="ada", is_verified=True)
            db.add(account)
            await db.flush()
            db.add(RetiredAddress(email=ADA, user_id=str(account.id), retired_at=datetime.now(UTC)))
    return await _sign_in(client, NEW), str(account.id)


async def _held_by_another(stored: str = ADA) -> str:
    """An account at ADA beside `_moved`'s retirement of it, as a sign-in on a
    release from before migration 0025 makes one, stored as `stored`. Returns
    its id."""
    async with async_session_maker() as db:
        async with db.begin():
            holder = User(email=stored, name="ada", is_verified=True)
            db.add(holder)
            await db.flush()
            return str(holder.id)


async def _bearer(node_id: str) -> dict[str, str]:
    """The header a registered node signs its own requests with."""
    async with async_session_maker() as db:
        async with db.begin():
            token = await node_auth.mint_token(db, node_id)
    return {"Authorization": f"Bearer {token}"}


async def _polled_radar(owner_id: str, owner_email: str = ADA) -> str:
    async with async_session_maker() as db:
        async with db.begin():
            registration = await create_polled_radar(db, **_radar_args(owner_user_id=owner_id, owner_email=owner_email))
    return registration.node.node_id


def _legacy_cookie(user_id: str) -> str:
    """A cookie as fastapi-users minted one (tests/test_sessions.py, _legacy_cookie)."""
    payload = {"sub": user_id, "aud": ["fastapi-users:auth"], "exp": datetime.now(UTC) + LEGACY_TOKEN_LIFETIME}
    return jwt.encode(payload, JWT_SECRET, "HS256")


class _AccessVerifier:
    def is_configured(self):
        return True

    async def identity(self, token):
        return "admin@example.com" if token else None


# ── The route ────────────────────────────────────────────────────────────────


class TestTheConfirmation:
    @pytest.mark.parametrize("typed", [BOB, "ada@mail.example.com", "ada", ""])
    async def test_an_address_that_is_not_the_accounts_is_400_and_deletes_nothing(self, client, typed):
        cookie = await _sign_in(client)
        ada_id = await _id_of()
        await own_registered_node(NODES[0], ada_id)

        r = _delete(client, cookie, typed)

        assert r.status_code == 400
        assert r.json() == WRONG_ADDRESS
        assert _set_cookies(r) == []
        assert await _account() is not None
        assert await _owned_by(ada_id) == {NODES[0]}
        assert _me(client, cookie).status_code == 200

    @pytest.mark.parametrize("typed", [ADA, "  ADA@Example.COM\t"])
    async def test_the_address_is_matched_trimmed_and_in_any_case(self, client, typed):
        cookie = await _sign_in(client)

        r = _delete(client, cookie, typed)

        assert r.status_code == 200
        assert r.json() == {"ok": True, "released": 0}
        assert _cleared(r)
        assert await _account() is None

    @pytest.mark.parametrize(
        "raw", [b"{}", b'{"email": null}', b'{"email": "ada@example.com", "really": true}', b"not json"]
    )
    async def test_a_body_that_is_not_one_address_is_422_and_deletes_nothing(self, client, raw):
        cookie = await _sign_in(client)

        r = client.post("/api/auth/me/delete", content=raw, headers={"Content-Type": "application/json", **_as(cookie)})

        assert r.status_code == 422
        assert await _account() is not None


class TestWhoMayDelete:
    async def test_a_signed_out_caller_is_401(self, client):
        await _sign_in(client)

        r = client.post("/api/auth/me/delete", json={"email": ADA})

        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}
        assert await _account() is not None

    async def test_a_deactivated_account_is_refused_and_keeps_its_nodes(self, client):
        cookie = await _sign_in(client)
        ada_id = await _id_of()
        await own_registered_node(NODES[0], ada_id)
        await sessions.deactivate_account(ada_id)

        r = _delete(client, cookie)

        assert r.status_code == 401
        assert await _account() is not None
        assert await _owned_by(ada_id) == {NODES[0]}

    def test_an_administrator_with_no_cookie_session_has_no_account_to_delete(self, client, monkeypatch):
        with patch("core.users.access_identity", _AccessVerifier()):
            access = client.post(
                "/api/auth/me/delete", json={"email": "admin@example.com"}, headers={"Cf-Access-Jwt-Assertion": "t"}
            )
        monkeypatch.setattr(users, "AUTH_BYPASS", True)
        anonymous = client.post("/api/auth/me/delete", json={"email": users.ANONYMOUS_USER["email"]})

        assert access.status_code == anonymous.status_code == 401

    async def test_a_legacy_cookie_deletes_its_account_too(self, client):
        ada = await users.get_or_create_magic_link_user(ADA)

        r = _delete(client, _legacy_cookie(str(ada.id)))

        assert r.status_code == 200
        assert _cleared(r)
        assert await _account() is None


# ── What a deletion does ─────────────────────────────────────────────────────


class TestWhatGoes:
    async def test_every_node_is_handed_back_as_its_owner_would_and_counted(self, client, poller_wakes):
        cookie = await _sign_in(client)
        ada_id = await _id_of()
        await own_registered_node(NODES[0], ada_id)
        await own_registered_node(NODES[1], ada_id)
        radar = await _polled_radar(ada_id)
        await _sign_in(client, BOB)
        bob_id = await _id_of(BOB)
        await own_registered_node(BOBS_NODE, bob_id)

        r = _delete(client, cookie)

        assert r.json() == {"ok": True, "released": 3}
        assert await _owned_by(ada_id) == set()
        for node_id in (NODES[0], NODES[1], radar):
            assert await get_node_owner(node_id) is None
        # A fleet node stays registered for its next owner to claim.
        (fleet_node,) = await _rows(Node, node_id=NODES[0])
        assert fleet_node.status != "retired"
        # A polled radar is removed, as its owner's release removes it.
        assert (await _rows(Node, node_id=radar))[0].status == "retired"
        assert await _rows(PolledRadar, node_id=radar) == []
        assert [(e.kind, e.actor) for e in await _rows(NodeEvent, node_id=radar)] == [("removed", ada_id)]
        assert poller_wakes == [True]
        # Somebody else's are left alone.
        assert await _owned_by(bob_id) == {BOBS_NODE}

    async def test_every_session_and_cookie_ends_and_open_sockets_are_told(self, client):
        laptop = await _sign_in(client)
        phone = await _sign_in(client)
        ada_id = await _id_of()
        legacy = _legacy_cookie(ada_id)
        bob = await _sign_in(client, BOB)
        watch = sessions.watch_account((await _account()).id)
        try:
            r = _delete(client, laptop)

            assert r.status_code == 200
            assert _cleared(r)
            assert await watch.wait(1) is True
        finally:
            watch.release()
        for cookie in (laptop, phone, legacy):
            assert _me(client, cookie).status_code == 401
        assert await _rows(UserSession, user_id=ada_id) == []
        assert await _rows(RetiredLegacyToken, user_id=ada_id) == []
        assert _me(client, bob).status_code == 200

    async def test_the_row_goes_and_the_deletion_is_recorded(self, client):
        cookie = await _sign_in(client)
        ada_id = await _id_of()
        await own_registered_node(NODES[0], ada_id)

        _delete(client, cookie)

        assert await _rows(User, email=ADA) == []
        (recorded,) = [e for e in admin._events if e["category"] == "user"]
        assert recorded["message"] == f"Account {ada_id} deleted"
        assert recorded["severity"] == "warning"
        assert recorded["meta"] == {"user_id": ada_id, "by": accounts.SELF, "nodes": [NODES[0]]}

    async def test_the_event_log_keeps_no_address_of_an_account_that_deleted_itself(self, client):
        """The log is kept on disk, and the account is named by its id. Its
        address, and the one it retired, go with the account."""
        cookie, ada_id = await _moved(client)

        _delete(client, cookie, NEW)

        admin._save_events()
        saved = admin._EVENTS_FILE.read_text()
        assert ada_id in saved
        assert ADA not in saved
        assert NEW not in saved

    async def test_a_sign_in_link_minted_before_signs_nobody_in_after(self, client):
        cookie = await _sign_in(client)
        stale = await create_magic_link(ADA)
        bobs = await create_magic_link(BOB)

        _delete(client, cookie)

        assert client.get(f"/api/auth/magic-link/{stale}").status_code == 404
        r = client.post("/api/auth/magic-link/consume", json={"token": stale})
        assert (r.status_code, r.json()) == (400, DEAD_SIGN_IN_LINK)
        assert await _account() is None
        assert await peek_magic_link(bobs, intent="signin") is not None

    async def test_a_fresh_sign_in_afterwards_opens_a_new_empty_account(self, client):
        cookie = await _sign_in(client)
        old_id = await _id_of()
        await own_registered_node(NODES[0], old_id)
        _delete(client, cookie)

        fresh = await _sign_in(client)

        me = _me(client, fresh).json()
        assert me["id"] != old_id
        assert me["name"] == "ada"
        assert client.get("/api/auth/me/nodes", headers=_as(fresh)).json() == []

    async def test_a_claim_link_to_the_address_is_left_for_the_node_and_lands_on_a_new_account(self, client):
        """A claim link is about a node, not the account: whoever is at the
        address can still claim or decline it, and a claim makes an account
        of its own, never the deleted one's."""
        cookie = await _sign_in(client)
        old_id = await _id_of()
        token = await _claim_link(ADA, NODES[2])

        _delete(client, cookie)
        r = client.post("/api/auth/claim/consume", json={"token": token})

        assert r.status_code == 200
        new_id = await _id_of()
        assert new_id != old_id
        assert await get_node_owner(NODES[2]) == new_id

    async def test_a_claim_link_forwarded_from_an_address_it_retired_dies_with_the_account(self, client, claim_mail):
        """Its mail went to the address the account has now, so whoever holds
        it proved that address and not the one the link names. After the
        deletion a click would make an account at the retired address,
        verified, owning the node and signed in. The links to the account's
        own address, and to anybody else's, are left as they are."""
        cookie, _ = await _moved(client)
        forwarded = await _mailed_claim_link(ADA, NODES[0])
        own = await _mailed_claim_link(NEW, NODES[1])
        bobs = await _mailed_claim_link(BOB, NODES[2])
        mailed = {row.node_id: row.recipient for row in await _rows(MailOutbox)}
        assert mailed == {NODES[0]: NEW, NODES[1]: NEW, NODES[2]: BOB}

        assert _delete(client, cookie, NEW).json() == {"ok": True, "released": 0}
        clicked = client.post("/api/auth/claim/consume", json={"token": forwarded})

        assert clicked.status_code == 400
        assert _set_cookies(clicked) == []
        assert await _account(ADA) is None
        assert await get_node_owner(NODES[0]) is None
        # The node reads unclaimed with its address on file, so whoever is at
        # it can ask again, and its mail does not go.
        assert [claim.email for claim in await _rows(NodeClaim, node_id=NODES[0])] == [ADA]
        assert await _rows(NodeClaimChallenge, node_id=NODES[0]) == []
        statuses = {row.node_id: row.status for row in await _rows(MailOutbox)}
        assert statuses == {NODES[0]: "cancelled", NODES[1]: "queued", NODES[2]: "queued"}
        assert await claim_links.peek(own) is not None
        assert await claim_links.peek(bobs) is not None

    async def test_a_node_whose_challenge_is_another_link_by_now_keeps_it(self, client, claim_mail):
        """Only a challenge still naming a link that dies goes with it. A node
        that has offered another address since holds that address's link, and
        its mail goes."""
        cookie, _ = await _moved(client)
        stray = await claim_links.issue(email=ADA, node_id=NODES[0], now=time.time())
        assert stray is not None
        current = await _mailed_claim_link(BOB, NODES[0])

        _delete(client, cookie, NEW)

        assert await claim_links.peek(stray.token) is None
        assert await claim_links.peek(current) is not None
        assert [c.handle for c in await _rows(NodeClaimChallenge, node_id=NODES[0])] == [
            claim_links.handle_for(current)
        ]
        assert [row.status for row in await _rows(MailOutbox, node_id=NODES[0])] == ["queued"]

    async def test_a_retired_address_another_account_has_as_its_own_keeps_its_links(self, client, claim_mail):
        """The state a sign-in during a rollback across 0025 leaves. The
        address is that account's and is mailed itself (core/users.py,
        forwarding_address), so whoever clicks its links proved it, and
        deleting the account that retired it takes nothing of the other's."""
        cookie, _ = await _moved(client)
        async with async_session_maker() as db:
            async with db.begin():
                db.add(User(email=ADA, name="ada", is_verified=True))
        theirs = await _mailed_claim_link(ADA, NODES[0])
        assert [row.recipient for row in await _rows(MailOutbox, node_id=NODES[0])] == [ADA]

        _delete(client, cookie, NEW)

        assert await claim_links.peek(theirs) is not None
        assert [c.handle for c in await _rows(NodeClaimChallenge, node_id=NODES[0])] == [claim_links.handle_for(theirs)]
        assert [row.status for row in await _rows(MailOutbox, node_id=NODES[0])] == ["queued"]

    @pytest.mark.parametrize("stored", [ADA, "Ada@Example.COM"], ids=["as-typed", "stored-in-capitals"])
    async def test_deleting_the_account_that_has_an_address_another_retired_frees_it(self, client, claim_mail, stored):
        """The same state, and the other account deleting itself. The other
        account's retirement of ADA goes too. Left, it would find that account
        for ADA, binding a node claimed with it there and signing ADA's reader
        in to an account at an address they never proved. ADA is nobody's
        afterwards, and a sign-in with it makes a new account.

        The claim links to ADA go with it, those mailed to ADA itself
        included: some went to the other account's address (the next test),
        and a link does not say which."""
        _cookie, moved_id = await _moved(client)
        holder_id = await _held_by_another(stored)
        cookie = await _sign_in(client, ADA)
        theirs = await _mailed_claim_link(ADA, NODES[0])
        assert [row.recipient for row in await _rows(MailOutbox, node_id=NODES[0])] == [ADA]

        assert _delete(client, cookie, ADA).json() == {"ok": True, "released": 0}

        assert await _rows(RetiredAddress, email=ADA) == []
        clicked = client.post("/api/auth/claim/consume", json={"token": theirs})
        assert clicked.status_code == 400
        assert _set_cookies(clicked) == []
        assert await get_node_owner(NODES[0]) is None
        assert [row.status for row in await _rows(MailOutbox, node_id=NODES[0])] == ["cancelled"]
        # The account that retired it is left as it was, at the address it moved to.
        assert await _id_of(NEW) == moved_id
        assert await _owned_by(moved_id) == set()
        made = _me(client, await _sign_in(client, ADA)).json()["id"]
        assert made not in (moved_id, holder_id)
        assert made == await _id_of(ADA)

    @pytest.mark.parametrize("stored", [ADA, "Ada@Example.COM"], ids=["as-typed", "stored-in-capitals"])
    async def test_a_claim_link_forwarded_before_it_had_the_address_signs_nobody_in_after(
        self, client, claim_mail, stored
    ):
        """A node offered ADA while ADA was only the moved account's retired
        address was mailed at NEW. The account at ADA came after, and deletes
        itself. With the retirement gone too, nothing ties the link to NEW
        any more, and a click would make an account at ADA, verified, owning
        the node and signed in, for somebody who proved NEW and not ADA. It
        dies with every other claim link to ADA. The node reads unclaimed with
        ADA on file, and asking again mails ADA itself. Other addresses' links
        are left as they are."""
        _cookie, moved_id = await _moved(client)
        forwarded = await _mailed_claim_link(ADA, NODES[0])
        bobs = await _mailed_claim_link(BOB, NODES[2])
        node = await _bearer(NODES[0])
        assert {row.node_id: row.recipient for row in await _rows(MailOutbox)} == {NODES[0]: NEW, NODES[2]: BOB}
        await _held_by_another(stored)
        cookie = await _sign_in(client, ADA)

        assert _delete(client, cookie, ADA).json() == {"ok": True, "released": 0}
        signed_in = {row.id for row in await _rows(UserSession)}

        assert client.get(f"/api/auth/claim/{forwarded}").status_code == 404
        clicked = client.post("/api/auth/claim/consume", json={"token": forwarded})
        assert (clicked.status_code, clicked.json()) == (400, {"detail": "That link is no longer valid"})
        assert _set_cookies(clicked) == []
        assert {row.id for row in await _rows(UserSession)} == signed_in
        assert [account.email for account in await _rows(User)] == [NEW]
        assert await _owned_by(moved_id) == set()
        assert await _rows(RetiredAddress, email=ADA) == []
        (claim,) = await _rows(NodeClaim, node_id=NODES[0])
        assert (claim.user_id, claim.email, claim.verified) == (None, ADA, False)
        assert await _rows(NodeClaimChallenge, node_id=NODES[0]) == []
        assert client.get("/v1/nodes/claim", headers=node).json() == {
            "state": "unclaimed",
            "email": ADA,
            "undeliverable": False,
        }
        statuses = {row.node_id: row.status for row in await _rows(MailOutbox)}
        assert statuses == {NODES[0]: "cancelled", NODES[2]: "queued"}
        assert await claim_links.peek(bobs) is not None

        asked = client.post("/v1/nodes/claim/resend", headers=node)

        assert (asked.status_code, asked.json()["state"]) == (200, "pending")
        mailed = {row.recipient: row.status for row in await _rows(MailOutbox, node_id=NODES[0])}
        assert mailed == {NEW: "cancelled", ADA: "queued"}

    async def test_a_deletion_stopped_by_a_node_leaves_the_forwarded_links_live(self, client, claim_mail, monkeypatch):
        """Refused for a node bound to it, the deletion changes nothing, these
        links included: the account still stands, and is still theirs."""
        _cookie, ada_id = await _moved(client)
        forwarded = await _mailed_claim_link(ADA, NODES[0])
        await own_registered_node(NODES[1], ada_id)

        with pytest.raises(sessions.AccountHoldsNodes):
            await sessions.remove_account(ada_id)

        assert await claim_links.peek(forwarded) is not None
        assert [row.status for row in await _rows(MailOutbox, node_id=NODES[0])] == ["queued"]
        assert len(await _rows(NodeClaimChallenge, node_id=NODES[0])) == 1


# ── delete_account, as an administrator's route will call it ─────────────────


class TestDeleteAccount:
    async def test_an_account_that_is_not_there_is_none_and_its_id_keeps_its_nodes(self):
        """A node can name an owner with no account row, an Access identity's id."""
        nobody = "11111111-1111-1111-1111-111111111111"
        await own_registered_node(NODES[0], nobody)

        assert await accounts.delete_account(nobody, by="admin@example.com") is None
        assert await accounts.delete_account("not-an-id", by="admin@example.com") is None
        assert await get_node_owner(NODES[0]) == nobody
        assert list(admin._events) == []

    async def test_an_administrator_deletes_a_deactivated_account_and_its_nodes_go(self):
        ada = await users.get_or_create_magic_link_user(ADA)
        await own_registered_node(NODES[0], str(ada.id))
        await sessions.deactivate_account(ada.id)

        assert await accounts.delete_account(str(ada.id), by="admin@example.com") == 1
        assert await _account() is None
        assert await get_node_owner(NODES[0]) is None
        assert admin._events[0]["meta"]["by"] == "admin@example.com"

    async def test_deleting_again_finds_nothing_and_changes_nothing(self):
        ada = await users.get_or_create_magic_link_user(ADA)
        assert await accounts.delete_account(ada.id, by=ADA) == 0

        assert await accounts.delete_account(ada.id, by=ADA) is None
        assert len([e for e in admin._events if e["category"] == "user"]) == 1

    async def test_two_deletions_at_once_leave_nothing_owned_and_record_one(self):
        """A form sent twice, or the owner and an administrator together."""
        ada = await users.get_or_create_magic_link_user(ADA)
        for node_id in NODES[:3]:
            await own_registered_node(node_id, str(ada.id))

        results = await asyncio.gather(
            accounts.delete_account(ada.id, by=ADA), accounts.delete_account(ada.id, by="admin@example.com")
        )

        assert all(result is None or isinstance(result, int) for result in results)
        assert await _owned_by(str(ada.id)) == set()
        assert await _account() is None
        assert len([e for e in admin._events if e["category"] == "user"]) == 1

    async def test_the_owner_release_route_and_a_deletion_hand_back_through_one_function(self, client, monkeypatch):
        cookie = await _sign_in(client)
        ada_id = await _id_of()
        await own_registered_node(NODES[0], ada_id)
        await own_registered_node(NODES[1], ada_id)
        handed_back = []
        release = accounts.release_owned_node

        async def watched(node_id, user_id):
            handed_back.append(node_id)
            return await release(node_id, user_id)

        monkeypatch.setattr(accounts, "release_owned_node", watched)

        assert client.delete(f"/api/auth/me/nodes/{NODES[0]}/claim", headers=_as(cookie)).json() == {"ok": True}
        assert _delete(client, cookie).json() == {"ok": True, "released": 1}
        assert handed_back == [NODES[0], NODES[1]]


# ── A claim racing the deletion ──────────────────────────────────────────────
#
# A claim checks its account is active in the write that binds the node,
# under SQLite's write lock (services/node_claiming.py, complete_claim). The
# deletion removes the row in a write that first checks no node names it
# (core/sessions.py, remove_account). The two cannot interleave, so either the
# claim committed first and the deletion sees its node, or the deletion
# committed first and the claim finds no account.


class TestARacingClaim:
    async def test_a_claim_that_lands_after_the_release_stops_the_removal_until_its_node_is_handed_back(
        self, client, monkeypatch
    ):
        cookie = await _sign_in(client)
        ada_id = await _id_of()
        await own_registered_node(NODES[0], ada_id)
        token = await _claim_link(ADA, NODES[1])
        remove = sessions.remove_account
        landed = []

        async def claim_lands_first(user_id, **kw):
            if not landed:
                landed.append(await complete_claim(token))
            return await remove(user_id, **kw)

        monkeypatch.setattr(sessions, "remove_account", claim_lands_first)

        r = _delete(client, cookie)

        ((outcome, claimant, _),) = landed
        assert (outcome, str(claimant.id)) == (ClaimOutcome.BOUND, ada_id)
        assert await _owned_by(ada_id) == set()
        assert await get_node_owner(NODES[1]) is None
        assert await _account() is None
        assert r.json() == {"ok": True, "released": 2}

    async def test_a_claim_that_read_the_account_before_the_deletion_cannot_bind_after_it(self, monkeypatch):
        ada = await users.get_or_create_magic_link_user(ADA)
        token = await _claim_link(ADA, NODES[1])
        look_up = node_claiming.get_or_create_magic_link_user

        async def deleted_meanwhile(email):
            account = await look_up(email)
            assert await accounts.delete_account(account.id, by=ADA) == 0
            return account

        monkeypatch.setattr(node_claiming, "get_or_create_magic_link_user", deleted_meanwhile)

        outcome, claimant, _ = await complete_claim(token)

        assert (outcome, claimant) == (ClaimOutcome.INVALID, None)
        assert await get_node_owner(NODES[1]) is None
        assert await _owned_by(str(ada.id)) == set()
        assert await _account() is None

    async def test_claims_that_keep_landing_stop_the_deletion_with_the_account_whole(self, client, monkeypatch):
        cookie = await _sign_in(client)
        ada_id = await _id_of()
        remove = sessions.remove_account
        arriving = iter(NODES)

        async def another_lands(user_id, **kw):
            await own_registered_node(next(arriving), ada_id)
            return await remove(user_id, **kw)

        monkeypatch.setattr(sessions, "remove_account", another_lands)

        r = _delete(client, cookie)

        assert r.status_code == 409
        assert r.json() == {"detail": "A node was claimed for this account while it was being deleted. Try again."}
        assert _set_cookies(r) == []
        assert (await _account()).is_active
        assert _me(client, cookie).status_code == 200
        # The last to land is the account's; those before it were handed back.
        assert await _owned_by(ada_id) == {NODES[2]}
        assert [await get_node_owner(n) for n in NODES[:2]] == [None, None]


# ── A deletion that fails part way ───────────────────────────────────────────
#
# The nodes go while the account can still sign in, and the row, its sessions
# and its sign-in links go together in one transaction at the end. So a
# failure anywhere leaves an account that signs in and owns whatever was not
# handed back yet, and the same function, run again by its owner or by an
# administrator, finishes the job.


class TestAFailurePartWay:
    async def test_a_failed_hand_back_leaves_an_account_that_signs_in_and_can_be_deleted_again(
        self, client, monkeypatch
    ):
        cookie = await _sign_in(client)
        ada_id = await _id_of()
        await own_registered_node(NODES[0], ada_id)
        await own_registered_node(NODES[1], ada_id)
        release = accounts.release_owned_node
        attempts = []

        async def fails_second(node_id, user_id):
            attempts.append(node_id)
            if len(attempts) == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return await release(node_id, user_id)

        monkeypatch.setattr(accounts, "release_owned_node", fails_second)

        r = _delete(client, cookie)

        assert r.status_code == 500
        assert _set_cookies(r) == []
        assert (await _account()).is_active
        assert _me(client, cookie).status_code == 200
        assert await _owned_by(ada_id) == {attempts[1]}
        assert await _sign_in(client)

        monkeypatch.setattr(accounts, "release_owned_node", release)
        assert await accounts.delete_account(ada_id, by="admin@example.com") == 1
        assert await _owned_by(ada_id) == set()
        assert await _account() is None

    async def test_a_failed_removal_changes_nothing_of_what_it_would_have_removed(self, client):
        laptop = await _sign_in(client)
        phone = await _sign_in(client)
        ada_id = await _id_of()
        stale = await create_magic_link(ADA)

        def refused(_conn, _cursor, statement, *_):
            if statement.startswith("DELETE FROM user "):
                raise sqlite3.OperationalError("disk I/O error")

        event.listen(users.engine.sync_engine, "before_cursor_execute", refused)
        try:
            with pytest.raises(sqlite3.OperationalError):
                await accounts.delete_account(ada_id, by=ADA)
        finally:
            event.remove(users.engine.sync_engine, "before_cursor_execute", refused)

        assert (await _account()).is_active
        assert len(await _rows(UserSession, user_id=ada_id)) == 2
        assert _me(client, laptop).status_code == _me(client, phone).status_code == 200
        assert await peek_magic_link(stale, intent="signin") is not None
        assert len(await _rows(MagicLink, email=ADA)) >= 1
