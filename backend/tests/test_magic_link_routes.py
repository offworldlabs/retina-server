"""The magic-link endpoints in routes/auth.py.

The invariant most of these exist to protect: /api/auth/magic-link answers the
same way for an address that owns receivers and one that has never been seen,
so it cannot be used to find out which addresses do. tests/test_magic_links.py
covers the token store underneath.

The mail takes its real path: the route queues it in the outbox table and the
app's own outbox worker, started by the lifespan, sends it. Only
services.mail.deliver is replaced, so nothing leaves the process.
"""

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import routes.auth as _auth
from core import users
from core.auth import (
    _MAX_OUTSTANDING_MAGIC_LINKS,
    INTENT_SIGNIN,
    MAGIC_LINK_EXPIRY_S,
    create_magic_link,
    peek_magic_link,
)
from core.database import DATABASE_URL
from main import app
from services import mail, mail_outbox, mail_templates
from services.claim_links import INTENT_CLAIM

_DB = Path(make_url(DATABASE_URL).database or "")


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _mail_configured(monkeypatch):
    """Mail resolves and the link has a host, and nothing is actually sent.

    HOST_APP belongs here rather than in the tests that assert on the link:
    without it the endpoint refuses outright, so every test that expects a 202
    depends on it being set."""
    monkeypatch.setenv("MAIL_TRANSPORT", "cloudflare")
    monkeypatch.setenv("MAIL_FROM", "RETINA <no-reply@retina.fm>")
    monkeypatch.setenv("CLOUDFLARE_EMAIL_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct123")
    monkeypatch.setenv("HOST_APP", "app.retina.fm")
    # The lifespan runs the bounce-feed task as well, which would pull from a
    # real queue if a backend/.env named one.
    monkeypatch.delenv("CLOUDFLARE_EMAIL_EVENTS_QUEUE_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_EMAIL_EVENTS_TOKEN", raising=False)


def _rows(table: str) -> list[dict]:
    with closing(sqlite3.connect(_DB)) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _empty_outbox() -> None:
    with closing(sqlite3.connect(_DB)) as con:
        con.execute("DELETE FROM mail_outbox")
        con.commit()


class Outbox:
    """The sign-in mail a test queued, and what the app's outbox worker sent.

    `deliver` stands in for services.mail.deliver, which the worker looks up for
    each send. The worker sends on a thread of its own once the response has
    gone, so a test asks for what was sent with `delivered`, which waits for the
    worker rather than reading a list that may not have caught up yet.
    """

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._sent: list[mail.Message] = []
        self._passes = 0
        self._pass_of_last_send = 0

    def deliver(self, message: mail.Message) -> mail.Delivery:
        with self._changed:
            self._sent.append(message)
            self._pass_of_last_send = self._passes
            message_id = f"test-{len(self._sent)}"
            self._changed.notify_all()
        return mail.Delivery(mail.SENT, "delivered", message_id=message_id)

    def pass_ended(self) -> None:
        """The worker has taken everything due, recorded it, and is about to wait."""
        with self._changed:
            self._passes += 1
            self._changed.notify_all()

    @staticmethod
    def queued() -> int:
        """Sign-in rows written during this test, whatever became of them.

        Exact once a test client's call has returned: the route queues the row
        after it answers, but within the request, which the client waits out.
        """
        return sum(row["kind"] == mail_outbox.KIND_SIGN_IN for row in _rows("mail_outbox"))

    def delivered(self, timeout: float = 10.0) -> list[mail.Message]:
        """What was sent, once every row queued so far has been sent and settled.

        Nothing queued means nothing to send, so that answers at once. A row
        that never goes fails the wait, and one sent twice is an extra message.
        """
        expected = self.queued()

        def settled() -> bool:
            if len(self._sent) < expected:
                return False
            # The pass that sent the last one records it before it ends.
            return expected == 0 or self._passes > self._pass_of_last_send

        with self._changed:
            assert self._changed.wait_for(settled, timeout), (
                f"{len(self._sent)} of {expected} queued sign-in mails sent within {timeout:.0f}s"
            )
            return list(self._sent)

    def idle(self, timeout: float = 10.0) -> None:
        """Wait until the worker has finished a pass and is waiting for work."""
        with self._changed:
            assert self._changed.wait_for(lambda: self._passes > 0, timeout), "the outbox worker never ran"


@pytest.fixture(autouse=True)
def sent(monkeypatch, tmp_path, _clean_db):
    """The outbox, empty and this test's own, sending to an Outbox.

    Emptied here because nothing else empties it between tests, and a row left
    over would be sent to this test's stand-in. The key goes in tmp_path rather
    than beside the shared test database.
    """
    _empty_outbox()
    monkeypatch.setattr(mail_outbox, "_key_path", lambda: tmp_path / mail_outbox.KEY_FILE_NAME)
    monkeypatch.setattr(mail_outbox, "_fernets", {})
    outbox = Outbox()
    monkeypatch.setattr(mail, "deliver", outbox.deliver)
    until_due = mail_outbox._until_due

    async def reporting(*args):
        wait = await until_due(*args)
        outbox.pass_ended()
        return wait

    monkeypatch.setattr(mail_outbox, "_until_due", reporting)
    yield outbox
    # A bare router has no worker, so what it queued is still there.
    _empty_outbox()


def _token(message: mail.Message) -> str:
    return message.text.split("/auth/link/")[1].split()[0].split("?")[0]


@pytest.fixture(autouse=True)
def _clear_source_quota():
    """The per-source window is module state shared by every test in the
    session, and TestClient presents the same client address to all of them."""
    _auth._magic_link_requests.clear()
    yield
    _auth._magic_link_requests.clear()


# ── Requesting a link ─────────────────────────────────────────────────────────


def _answer(response) -> tuple:
    """Everything a caller can read off a response, byte for byte."""
    return response.status_code, response.headers.raw, response.content


async def _post_as_a_server_would(application, payload: dict, events: list[str]) -> tuple[int, bytes]:
    """POST `payload` to the sign-in route by raw ASGI, and note in `events`
    the moment the response is complete.

    A test client hands back the response only once the application returns,
    so it cannot tell an answer that waited on some work from one that went
    first. Here the last `http.response.body` message is seen as it is sent.
    As uvicorn does, `receive` says `http.disconnect` once the response is
    complete, and the call returns when the application does.
    """
    body = json.dumps(payload).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/auth/magic-link",
        "raw_path": b"/api/auth/magic-link",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"app.retina.fm"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("192.0.2.1", 50000),
        "server": ("app.retina.fm", 443),
    }
    answered = asyncio.Event()
    asked = False
    status = 0
    chunks: list[bytes] = []

    async def receive():
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": body, "more_body": False}
        await answered.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                events.append("answered")
                answered.set()

    await application(scope, receive, send)
    return status, b"".join(chunks)


class TestRequestMagicLink:
    def test_accepts_and_mails_a_link(self, client, sent):
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        assert r.status_code == 202
        (message,) = sent.delivered()
        assert message.to == "owner@example.com"
        assert "/auth/link/" in message.text

    def test_the_mail_reads_as_written(self, client, sent):
        client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        (message,) = sent.delivered()
        link = f"https://app.retina.fm/auth/link/{_token(message)}"
        assert message.subject == "Sign in to RETINA"
        assert message.text == (
            "A request has been made to sign in to RETINA with this email address.\n\n"
            "Click this link to log in:\n"
            f"{link}\n\n"
            "The link works once and expires in 15 minutes.\n\n"
            "If this wasn't you, you may safely ignore this email."
        )
        assert message.html == mail_templates.sign_in(link).html

    def test_the_mail_goes_through_the_outbox_and_is_sent_once(self, client, sent):
        """One row, sent once by the lifespan's worker, both parts carrying the
        one live link, and nothing of the link left in the row once it is sent."""
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com", "next": "/settings"})
        assert r.status_code == 202

        (message,) = sent.delivered()
        token = _token(message)
        link = f"https://app.retina.fm/auth/link/{token}?next=/settings"
        rendered = mail_templates.sign_in(link)
        assert message == mail.Message(
            to="owner@example.com", subject=rendered.subject, text=rendered.text, html=rendered.html
        )
        assert link in message.text.splitlines()
        # The button and the link written out beneath it.
        assert message.html.count(f'href="{link}"') == 2

        (row,) = _rows("mail_outbox")
        assert row["kind"] == mail_outbox.KIND_SIGN_IN
        assert row["recipient"] == "owner@example.com"
        assert row["status"] == row["outcome"] == "sent"
        assert row["attempts"] == 1
        assert row["message_id"] == "test-1"
        assert row["body"] is None
        assert token not in json.dumps(row)

        # The mail dies with its link, both from one clock reading.
        (issued,) = _rows("magic_links")
        assert row["created_at"] == issued["created_at"]
        assert row["expires_at"] == issued["expires_at"] == issued["created_at"] + MAGIC_LINK_EXPIRY_S
        assert client.get(f"/api/auth/magic-link/{token}").json() == {"email": "owner@example.com"}

    def test_the_request_wakes_the_worker(self, sent, monkeypatch):
        """Idle, the worker would not look again for two minutes, so mail that
        goes within seconds went because the request woke it."""
        monkeypatch.setattr(mail_outbox, "IDLE_WAIT_S", 120.0)
        with TestClient(app, raise_server_exceptions=False) as client:
            sent.idle()
            r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
            assert r.status_code == 202
            (message,) = sent.delivered(timeout=10.0)
        assert message.to == "owner@example.com"

    def test_the_mailed_link_carries_the_token_and_nothing_else_does(self, client, sent):
        client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        (message,) = sent.delivered()
        assert len(_token(message)) > 20

    def test_the_response_body_never_carries_the_token(self, client, sent):
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        (message,) = sent.delivered()
        assert _token(message) not in r.text

    def test_the_link_host_comes_from_configuration_not_the_request(self, client, sent, monkeypatch):
        """Otherwise: ask for a link to somebody else's address with a forged
        Host header, and the mail they receive points at your server carrying a
        working token."""
        monkeypatch.setenv("HOST_APP", "app.retina.fm")
        client.post(
            "/api/auth/magic-link",
            json={"email": "victim@example.com"},
            headers={"Host": "evil.example.com"},
        )
        (message,) = sent.delivered()
        for part in (message.text, message.html):
            assert "https://app.retina.fm/auth/link/" in part
            assert "evil.example.com" not in part

    def test_the_link_lands_at_the_consoles_root(self, client, sent, monkeypatch):
        """HOST_APP serves the console at /, and it has no page under /dash/."""
        monkeypatch.setenv("HOST_APP", "app.retina.fm")
        client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        (message,) = sent.delivered()
        for part in (message.text, message.html):
            assert "https://app.retina.fm/auth/link/" in part
            assert "/dash/" not in part

    def test_an_unknown_address_gets_the_same_answer_as_a_known_one(self, client):
        """The whole point. A differing status, body or shape here says which
        addresses have accounts."""
        first = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        client.post("/api/auth/magic-link/consume", json={"token": "x"})
        second = client.post("/api/auth/magic-link", json={"email": "nobody@example.com"})
        assert first.status_code == second.status_code == 202
        assert first.json() == second.json()

    async def test_every_caller_gets_the_same_bytes(self, client, sent):
        """An address nobody has seen, one with an account, one holding every
        link it may, and a caller past its quota: nothing in the four answers
        tells them apart, though only the first two are mailed."""
        await users.get_or_create_magic_link_user("known@example.com")
        for _ in range(_MAX_OUTSTANDING_MAGIC_LINKS):
            await create_magic_link("capped@example.com")

        def ask(address: str):
            return client.post("/api/auth/magic-link", json={"email": address})

        answers = {name: ask(f"{name}@example.com") for name in ("unknown", "known", "capped")}
        assert sent.queued() == 2
        # The rest of this source's quota, so the next request is past it.
        fillers = _auth._MAX_MAGIC_LINK_REQUESTS_PER_SOURCE - len(answers)
        for _ in range(fillers):
            ask("filler@example.com")
        answers["over-quota"] = ask("late@example.com")
        assert sent.queued() == 2 + fillers

        assert answers["unknown"].status_code == 202
        assert answers["unknown"].json() == {"status": "accepted"}
        for name, answer in answers.items():
            assert _answer(answer) == _answer(answers["unknown"]), name
        assert sorted(message.to for message in sent.delivered()) == sorted(
            ["unknown@example.com", "known@example.com"] + ["filler@example.com"] * fillers
        )

    @pytest.mark.parametrize("withdrawal_fails", [False, True], ids=["withdrawn", "withdrawal-fails"])
    def test_a_mail_that_cannot_be_queued_is_answered_as_one_that_was(
        self, client, sent, monkeypatch, caplog, withdrawal_fails
    ):
        """A different answer would say the address was given a token. The
        link is killed, since no mail carries it, and the log says what failed
        without the link, even when the exception quotes it."""
        caplog.set_level(logging.INFO)
        minted: list[str] = []
        create = _auth.create_magic_link_in

        async def recording(session, email, **kwargs):
            token = await create(session, email, **kwargs)
            minted.append(token)
            return token

        monkeypatch.setattr(_auth, "create_magic_link_in", recording)
        queued = client.post("/api/auth/magic-link", json={"email": "someone@example.com"})

        async def refusing(_session, message, **_kwargs):
            raise RuntimeError(f"cannot queue {message.text}")

        async def unkillable(handle):
            raise RuntimeError(f"cannot withdraw {handle}")

        monkeypatch.setattr(mail_outbox, "enqueue", refusing)
        if withdrawal_fails:
            monkeypatch.setattr(_auth, "invalidate_magic_link", unkillable)
        failed = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})

        assert _answer(failed) == _answer(queued)
        token = minted[1]
        assert token not in caplog.text
        assert "cannot queue" not in caplog.text
        assert "Could not queue a sign-in mail (RuntimeError); withdrawing its link" in caplog.text
        (message,) = sent.delivered()
        assert message.to == "someone@example.com"
        assert [row["recipient"] for row in _rows("mail_outbox")] == ["someone@example.com"]
        if withdrawal_fails:
            assert "cannot withdraw" not in caplog.text
            assert "Could not withdraw an unsent sign-in link (RuntimeError)" in caplog.text
        else:
            assert client.get(f"/api/auth/magic-link/{token}").status_code == 404
            assert [row["email"] for row in _rows("magic_links")] == ["someone@example.com"]

    def test_a_capped_address_still_gets_202(self, client, sent):
        """Reaching the outstanding-link cap must not be visible either: it
        would say the address had been asked for before."""
        with patch.object(_auth, "create_magic_link_in", return_value=None):
            r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        assert r.status_code == 202
        assert sent.delivered() == []

    def test_a_malformed_address_is_rejected(self, client, sent):
        r = client.post("/api/auth/magic-link", json={"email": "not-an-address"})
        assert r.status_code == 422
        assert sent.delivered() == []

    @pytest.mark.parametrize("cause", ["no-transport", "no-host-app", "unusable-key"])
    def test_unavailable_refuses_before_minting_or_queueing(self, client, sent, monkeypatch, tmp_path, caplog, cause):
        """About the deployment, never the address, so it is safe to say. Without
        HOST_APP the only other source of a host is the request, and the caller
        controls that: refusing beats mailing a link to wherever the Host header
        said. Checked before a token is minted, so nothing is left behind."""
        if cause == "no-transport":
            monkeypatch.delenv("MAIL_TRANSPORT")
            reason = "mail is not configured"
        elif cause == "no-host-app":
            monkeypatch.delenv("HOST_APP")
            reason = "HOST_APP is not set"
        else:
            key = tmp_path / "unusable" / mail_outbox.KEY_FILE_NAME
            key.parent.mkdir()
            key.write_text("not a key")
            monkeypatch.setattr(mail_outbox, "_key_path", lambda: key)
            reason = "the outbox key is unusable"

        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})

        assert r.status_code == 503
        assert r.json() == {"detail": "Sign-in by email is unavailable"}
        assert f"Sign-in by email is unavailable: {reason}" in caplog.text
        assert _rows("magic_links") == []
        assert _rows("mail_outbox") == []
        assert sent.delivered() == []

    def test_no_session_cookie_is_set_by_asking(self, client):
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        assert "auth_token" not in r.cookies


@pytest.mark.asyncio
class TestTheAnswerGoesFirst:
    """How long the request takes is as easy to read as what it says.

    Minting a link writes rows, an address at its cap writes none, and a
    deactivated account none either, once core/auth.py refuses one. So the
    response is complete before anything looks at the address. Driven through
    the whole app, its middleware included, since what a caller can time is
    the last message the server is handed.
    """

    @staticmethod
    async def _make(kind: str, address: str) -> None:
        if kind in ("known", "deactivated"):
            user = await users.get_or_create_magic_link_user(address)
            if kind == "deactivated":
                async with users.async_session_maker() as session:
                    await session.execute(update(users.User).where(users.User.id == user.id).values(is_active=False))
                    await session.commit()
        elif kind == "capped":
            for _ in range(_MAX_OUTSTANDING_MAGIC_LINKS):
                await create_magic_link(address)

    @staticmethod
    def _record_minting(monkeypatch, events: list[str]) -> list[str | None]:
        """Note in `events` when a link is asked for, and return what each ask minted."""
        minted: list[str | None] = []
        create = _auth.create_magic_link_in

        async def minting(session, email, **kwargs):
            events.append("minting")
            token = await create(session, email, **kwargs)
            minted.append(token)
            return token

        monkeypatch.setattr(_auth, "create_magic_link_in", minting)
        return minted

    @pytest.mark.parametrize("kind", ["fresh", "known", "capped", "deactivated"])
    async def test_the_response_is_complete_before_the_address_is_looked_at(self, sent, monkeypatch, kind):
        address = f"{kind}@example.com"
        await self._make(kind, address)
        events: list[str] = []
        minted = self._record_minting(monkeypatch, events)

        status, body = await _post_as_a_server_would(app, {"email": address}, events)

        assert (status, body) == (202, b'{"status":"accepted"}')
        assert events == ["answered", "minting"]
        # Done all the same by the time the call returns, which is what a
        # server's graceful shutdown waits for. Whether a deactivated account
        # is mailed is core/auth.py's to decide; the order above is the point.
        (token,) = minted
        if kind in ("fresh", "known"):
            assert token is not None
        elif kind == "capped":
            assert token is None
        if token is None:
            assert sent.queued() == 0
        else:
            assert sent.queued() == 1
            link = await peek_magic_link(token, intent=INTENT_SIGNIN)
            assert link is not None
            assert link.email == address

    async def test_the_address_waits_until_the_server_has_the_whole_answer(self, sent, monkeypatch):
        """Not only until the route has handed it on. A middleware can take the
        last message at once and pass it on later, as BaseHTTPMiddleware does
        through a stream; this one does that and nothing else, so the order
        cannot come from how its tasks happen to be scheduled."""
        events: list[str] = []
        minted = self._record_minting(monkeypatch, events)
        router_app = FastAPI()
        router_app.include_router(_auth.router)

        async def passing_on_later(scope, receive, send):
            held: asyncio.Queue = asyncio.Queue()

            async def pass_on():
                while True:
                    message = await held.get()
                    await send(message)
                    if message["type"] == "http.response.body" and not message.get("more_body", False):
                        return

            passing = asyncio.create_task(pass_on())
            await router_app(scope, receive, held.put)
            await passing

        status, body = await _post_as_a_server_would(passing_on_later, {"email": "owner@example.com"}, events)

        assert (status, body) == (202, b'{"status":"accepted"}')
        assert events == ["answered", "minting"]
        (token,) = minted
        assert token is not None
        assert sent.queued() == 1

    @pytest.mark.parametrize("failing", ["mint", "queue"])
    async def test_a_failure_after_the_answer_is_logged_and_kept_from_the_server(
        self, sent, monkeypatch, caplog, failing
    ):
        """Nobody hears of it but the log, which says what failed without the
        address or the link, even when the exception quotes them. Through a
        bare router, since the app's BaseHTTPMiddleware swallows whatever
        escapes after the answer and would hide an escape here. A link minted
        for a mail that could not be queued is withdrawn."""
        caplog.set_level(logging.INFO)
        minted: list[str] = []
        create = _auth.create_magic_link_in

        async def minting(session, email, **kwargs):
            if failing == "mint":
                raise OperationalError(f"INSERT {email}", {}, Exception("database is locked"))
            token = await create(session, email, **kwargs)
            minted.append(token)
            return token

        async def refusing(_session, message, **_kwargs):
            raise RuntimeError(f"cannot queue to {message.to}: {message.text}")

        monkeypatch.setattr(_auth, "create_magic_link_in", minting)
        monkeypatch.setattr(mail_outbox, "enqueue", refusing)
        application = FastAPI()
        application.include_router(_auth.router)

        status, body = await _post_as_a_server_would(application, {"email": "someone@example.com"}, [])

        assert (status, body) == (202, b'{"status":"accepted"}')
        errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
        assert errors == [
            "Could not issue a sign-in link (OperationalError)"
            if failing == "mint"
            else "Could not queue a sign-in mail (RuntimeError); withdrawing its link"
        ]
        assert len(minted) == (failing == "queue")
        for leaked in ["someone@example.com", "database is locked", "cannot queue", *minted]:
            assert leaked not in caplog.text
        assert _rows("mail_outbox") == []
        assert _rows("magic_links") == []


class TestSignInDestination:
    """The page the console opens once the link is redeemed.

    Anyone can ask for a link to anyone's address, so `next` is attacker-chosen
    text in a genuine mail from us. It is only ever a plain path on HOST_APP."""

    @staticmethod
    def _link(sent):
        """The link the mail carries, the same in both parts."""
        (message,) = sent.delivered()
        link = next(line for line in message.text.splitlines() if "/auth/link/" in line)
        assert f'href="{link}"' in message.html
        return link

    @pytest.mark.parametrize("path", ["/settings", "/nodes/ret-0042", "/sim/physics", "/data/2026/09/17"])
    def test_the_link_carries_it(self, client, sent, path):
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com", "next": path})
        assert r.status_code == 202
        link = self._link(sent)
        token = link.split("/auth/link/")[1].split("?")[0]
        assert link == f"https://app.retina.fm/auth/link/{token}?next={path}"

    def test_without_one_the_link_is_bare(self, client, sent):
        client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        assert "?" not in self._link(sent)

    @pytest.mark.parametrize(
        "path",
        [
            "//evil.example.com",
            "//evil.example.com/settings",
            "/\\evil.example.com",
            "https://evil.example.com/",
            "javascript:alert(1)",
            "settings",
            "/settings?tab=security",
            "/settings#danger",
            "/set tings",
            "/%2F%2Fevil.example.com",
            "/" + "a" * 300,
            "",
        ],
    )
    def test_anything_but_a_plain_path_is_dropped_and_the_link_still_sent(self, client, sent, path):
        """Dropped rather than refused: a bad destination costs the visitor the
        page they wanted, not the sign-in."""
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com", "next": path})
        assert r.status_code == 202
        assert len(sent.delivered()) == 1
        assert "?" not in self._link(sent)

    def test_it_does_not_change_the_answer(self, client):
        """The response still says nothing about the address or the request."""
        with_next = client.post("/api/auth/magic-link", json={"email": "owner@example.com", "next": "/settings"})
        without = client.post("/api/auth/magic-link", json={"email": "someone@example.com"})
        assert with_next.status_code == without.status_code == 202
        assert with_next.json() == without.json()


class TestSourceQuota:
    def test_beyond_the_quota_nothing_is_mailed(self, client, sent):
        for _ in range(_auth._MAX_MAGIC_LINK_REQUESTS_PER_SOURCE):
            client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        before = len(sent.delivered())
        r = client.post("/api/auth/magic-link", json={"email": "someone@example.com"})
        assert r.status_code == 202, "the quota must not be visible in the response"
        assert len(sent.delivered()) == before

    def test_a_full_source_table_refuses_new_sources(self, client, sent, monkeypatch):
        """The table is bounded, or a flood from many addresses grows it until
        the process dies. Full, sign-in stops for a window rather than for good."""
        monkeypatch.setattr(_auth, "_MAX_MAGIC_LINK_SOURCES", 0)
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        assert r.status_code == 202
        assert sent.delivered() == []

    def test_the_quota_counts_requests_not_addresses(self, client, sent):
        """Otherwise it bounds nothing: the abuse is mailing many strangers."""
        for i in range(_auth._MAX_MAGIC_LINK_REQUESTS_PER_SOURCE):
            client.post("/api/auth/magic-link", json={"email": f"target{i}@example.com"})
        before = len(sent.delivered())
        client.post("/api/auth/magic-link", json={"email": "one-more@example.com"})
        assert len(sent.delivered()) == before


def _peer(application, source, *, trusted_proxy=False):
    """A caller whose transport peer is `source`, whatever headers it sends."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    target = ProxyHeadersMiddleware(application, trusted_hosts="127.0.0.1") if trusted_proxy else application

    async def connected(scope, receive, send):
        await target({**scope, "client": (source, 12345)}, receive, send)

    return TestClient(connected, base_url="https://dash.example.test")


class TestSourceDerivation:
    """What counts as one source. The quota above bounds nothing unless a caller
    is unable to widen it, so the address comes from the transport peer after the
    ASGI server's trusted-proxy handling and never from a forwarding header.
    """

    @pytest.fixture()
    def router_app(self, monkeypatch):
        """Only the auth router, with no token store behind it."""
        monkeypatch.setattr(_auth, "create_magic_link_in", AsyncMock(return_value="a-token"))
        application = FastAPI()
        application.include_router(_auth.router)
        return application

    @staticmethod
    def _mailed(caller, sent, **kwargs):
        """Whether asking for a link got one queued, without reading the
        response: the answer is 202 either way, which is the point of the
        endpoint. Queued rather than sent, since a bare router runs no worker."""
        before = sent.queued()
        response = caller.post("/api/auth/magic-link", json={"email": "owner@example.com"}, **kwargs)
        assert response.status_code == 202
        return sent.queued() > before

    def test_a_forwarding_header_from_an_untrusted_peer_buys_no_quota(self, router_app, sent, monkeypatch):
        monkeypatch.setattr(_auth, "_MAX_MAGIC_LINK_REQUESTS_PER_SOURCE", 1)
        with _peer(router_app, "192.0.2.1") as caller:
            assert self._mailed(caller, sent)
            for suffix in range(4):
                spoofed = f"198.51.100.{suffix}"
                assert not self._mailed(
                    caller,
                    sent,
                    headers={
                        "X-Forwarded-For": spoofed,
                        "X-Real-IP": spoofed,
                        "CF-Connecting-IP": spoofed,
                        "Host": f"surface-{suffix}.example.test",
                    },
                )
        with _peer(router_app, "192.0.2.2") as other:
            assert self._mailed(other, sent)

    def test_trusted_proxy_uses_last_untrusted_hop(self, router_app, sent, monkeypatch):
        """Deployed, nginx is the peer and appends the address it validated, so
        the rightmost hop it did not add is the client."""
        monkeypatch.setattr(_auth, "_MAX_MAGIC_LINK_REQUESTS_PER_SOURCE", 1)
        with _peer(router_app, "127.0.0.1", trusted_proxy=True) as proxy:
            assert self._mailed(proxy, sent, headers={"X-Forwarded-For": "198.51.100.1, 192.0.2.1"})
            assert not self._mailed(proxy, sent, headers={"X-Forwarded-For": "198.51.100.2, 192.0.2.1"})
            assert self._mailed(proxy, sent, headers={"X-Forwarded-For": "198.51.100.1, 192.0.2.2"})

    @pytest.mark.parametrize(
        ("first", "same_source", "other"),
        [
            ("2001:db8:1:2::1", "2001:db8:1:2::2", "2001:db8:1:3::1"),
            ("192.0.2.1", "::ffff:192.0.2.1", "192.0.2.2"),
        ],
    )
    def test_address_variants_share_quota(self, router_app, sent, monkeypatch, first, same_source, other):
        """One /64 is one source, and privacy-address rotation inside it moves
        nobody to a fresh quota. A v4-mapped address is the v4 address."""
        monkeypatch.setattr(_auth, "_MAX_MAGIC_LINK_REQUESTS_PER_SOURCE", 1)
        with _peer(router_app, first) as caller:
            assert self._mailed(caller, sent)
        with _peer(router_app, same_source) as caller:
            assert not self._mailed(caller, sent)
        with _peer(router_app, other) as caller:
            assert self._mailed(caller, sent)


# ── Previewing a link ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPreviewMagicLink:
    """The page names the address and waits for a press before it spends the
    token, so a scanner that renders the page leaves the link alive."""

    async def test_it_names_the_address_without_spending_the_link(self, client):
        token = await create_magic_link("Owner@Example.com")
        first = client.get(f"/api/auth/magic-link/{token}")
        second = client.get(f"/api/auth/magic-link/{token}")
        assert first.status_code == second.status_code == 200
        assert first.json() == {"email": "owner@example.com"}
        assert client.post("/api/auth/magic-link/consume", json={"token": token}).status_code == 200

    async def test_it_opens_no_session(self, client):
        token = await create_magic_link("owner@example.com")
        assert "auth_token" not in client.get(f"/api/auth/magic-link/{token}").cookies

    async def test_a_spent_link_reads_as_one_never_issued(self, client):
        token = await create_magic_link("owner@example.com")
        client.post("/api/auth/magic-link/consume", json={"token": token})
        spent = client.get(f"/api/auth/magic-link/{token}")
        never = client.get("/api/auth/magic-link/never-issued")
        assert spent.status_code == never.status_code == 404
        assert spent.json() == never.json()

    async def test_an_expired_link_is_refused(self, client):
        token = await create_magic_link("owner@example.com", now=time.time() - 3600)
        assert client.get(f"/api/auth/magic-link/{token}").status_code == 404

    async def test_a_claim_link_reads_as_one_never_issued(self, client):
        """Sign-in and claim tokens share one table. A claim link's preview is
        the claim route's to give, and naming its address here would let a
        forwarded claim link pass for a sign-in."""
        token = await create_magic_link("owner@example.com", intent=INTENT_CLAIM, node_id="ret-0001")
        assert client.get(f"/api/auth/magic-link/{token}").status_code == 404

    @pytest.mark.parametrize("token", ["never-issued", "   ", "a" * 200, "%00"])
    async def test_a_bad_token_is_refused_the_same_way(self, client, token):
        assert client.get(f"/api/auth/magic-link/{token}").status_code == 404


# ── Redeeming a link ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestConsumeMagicLink:
    async def test_redeeming_opens_a_session(self, client):
        token = await create_magic_link("owner@example.com")
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert r.status_code == 200
        assert r.json()["user"]["email"] == "owner@example.com"
        assert "auth_token" in r.cookies

    async def test_the_cookie_is_a_session_for_that_person(self, client):
        """Read the cookie back through the same strategy the WebSocket
        handshake uses, rather than through /api/auth/me: the suite runs with
        AUTH_ALLOW_ANONYMOUS_ADMIN=1 (conftest), so that route answers with the
        anonymous admin whatever cookie is presented and would pass here for
        the wrong reason."""
        from core.users import read_user_from_token

        token = await create_magic_link("owner@example.com")
        r = client.post("/api/auth/magic-link/consume", json={"token": token})

        user = await read_user_from_token(r.cookies["auth_token"])
        assert user is not None
        assert user.email == "owner@example.com"
        assert user.is_superuser is False

    async def test_the_cookie_is_httponly_and_secure(self, client):
        """It is the whole session and /ws/aircraft/owner reads it off the
        handshake, so script access to it would be script access to the feed."""
        token = await create_magic_link("owner@example.com")
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        header = r.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "Secure" in header

    async def test_the_account_is_never_an_administrator(self, client):
        """Admin is Cloudflare Access. Receiving mail is not a claim to it."""
        token = await create_magic_link("owner@example.com")
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        user = r.json()["user"]
        assert user["is_superuser"] is False
        assert user["role"] == "user"

    async def test_the_signed_in_user_carries_what_me_would_say_about_the_fleet(self, client, monkeypatch):
        """The page adopts this user without asking /me, so it must not lack
        anything /me would have told it: the physics layer reads the fleet flag."""
        monkeypatch.setenv("SYNTHETIC_FLEET_ENABLED", "1")
        token = await create_magic_link("owner@example.com")
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert r.json()["user"]["synthetic_fleet"] is True

    async def test_the_sign_in_is_the_last_visit_the_users_page_lists(self, client):
        before = time.time()
        token = await create_magic_link("owner@example.com")
        signed_in = client.post("/api/auth/magic-link/consume", json={"token": token}).json()["user"]

        (listed,) = client.get("/api/admin/users").json()["users"]
        assert before <= listed["last_seen_at"] <= time.time()
        # The sign-in page adopts the user it is handed, so it must carry what the row holds.
        assert signed_in["last_seen_at"] == listed["last_seen_at"]

    async def test_a_second_redemption_fails(self, client):
        token = await create_magic_link("owner@example.com")
        client.post("/api/auth/magic-link/consume", json={"token": token})
        again = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert again.status_code == 400

    @pytest.mark.parametrize("token", ["never-issued", "", "   ", "a" * 200, "../../etc/passwd", "%00"])
    async def test_a_bad_token_is_refused_the_same_way(self, client, token):
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert r.status_code == 400
        assert "auth_token" not in r.cookies

    async def test_the_failure_message_does_not_distinguish_the_reason(self, client):
        """Unknown, expired and already-redeemed must read identically, or the
        message says which guesses were once real."""
        token = await create_magic_link("owner@example.com")
        client.post("/api/auth/magic-link/consume", json={"token": token})
        replayed = client.post("/api/auth/magic-link/consume", json={"token": token})
        never = client.post("/api/auth/magic-link/consume", json={"token": "never-issued"})
        assert replayed.status_code == never.status_code
        assert replayed.json() == never.json()

    async def test_redeeming_twice_does_not_create_two_accounts(self, client):
        first = await create_magic_link("owner@example.com")
        one = client.post("/api/auth/magic-link/consume", json={"token": first})
        second = await create_magic_link("owner@example.com")
        two = client.post("/api/auth/magic-link/consume", json={"token": second})
        assert one.json()["user"]["id"] == two.json()["user"]["id"]

    async def test_a_get_on_the_consume_route_does_not_redeem(self, client):
        """The mailed URL is a GET that a mail scanner may prefetch. Only the
        POST from the page may consume, or the token is gone before the
        recipient clicks it."""
        token = await create_magic_link("owner@example.com")
        assert client.get(f"/api/auth/magic-link/consume?token={token}").status_code in (404, 405)
        assert client.post("/api/auth/magic-link/consume", json={"token": token}).status_code == 200


# ── Visits ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestVisits:
    """Every use of a session is a visit, written at most once per window."""

    @staticmethod
    async def _signed_in(client) -> str:
        token = await create_magic_link("owner@example.com")
        return client.post("/api/auth/magic-link/consume", json={"token": token}).cookies["auth_token"]

    @staticmethod
    async def _set_account(**values) -> None:
        async with users.async_session_maker() as session:
            user = (await session.execute(select(users.User))).scalars().one()
            for name, value in values.items():
                setattr(user, name, value)
            await session.commit()

    @staticmethod
    async def _last_visit() -> datetime | None:
        async with users.async_session_maker() as session:
            return (await session.execute(select(users.User))).scalars().one().last_seen_at

    async def test_a_request_carrying_the_session_records_a_visit(self, client, monkeypatch):
        """Through /me rather than the token reader, so the path a page load
        takes is the one tested. The bypass is off so the cookie is read at all."""
        cookie = await self._signed_in(client)
        await self._set_account(last_seen_at=datetime.now(UTC) - timedelta(hours=1))
        before = datetime.now(UTC)
        monkeypatch.setattr(users, "AUTH_BYPASS", False)

        # By header: the cookie is Secure and the test client speaks plain HTTP.
        r = client.get("/api/auth/me", headers={"Cookie": f"auth_token={cookie}"})

        assert r.json()["email"] == "owner@example.com"
        assert await self._last_visit() >= before

    async def test_a_session_used_again_within_the_window_is_not_written_again(self, client):
        cookie = await self._signed_in(client)
        recent = datetime.now(UTC) - timedelta(minutes=1)
        await self._set_account(last_seen_at=recent)

        await users.read_user_from_token(cookie)

        assert await self._last_visit() == recent

    async def test_a_deactivated_account_records_no_visit(self, client):
        cookie = await self._signed_in(client)
        earlier = datetime.now(UTC) - timedelta(hours=1)
        await self._set_account(last_seen_at=earlier, is_active=False)

        await users.read_user_from_token(cookie)

        assert await self._last_visit() == earlier

    async def test_a_deactivated_account_signing_in_records_no_visit(self, client):
        await users.get_or_create_magic_link_user("owner@example.com")
        await self._set_account(is_active=False)

        token = await create_magic_link("owner@example.com")
        client.post("/api/auth/magic-link/consume", json={"token": token})

        assert await self._last_visit() is None

    async def test_an_account_deactivated_under_a_request_records_no_visit(self, client):
        """The instance a request holds is its own snapshot of the row."""
        snapshot = await users.get_or_create_magic_link_user("owner@example.com")
        await self._set_account(is_active=False)

        await users.record_visit(snapshot)

        assert await self._last_visit() is None
        assert snapshot.last_seen_at is None

    async def test_a_sign_in_that_cannot_mint_its_session_records_no_visit(self, client, monkeypatch):
        async def unopened(user, **_kwargs):
            raise RuntimeError("no session store")

        monkeypatch.setattr(_auth.sessions, "open_session", unopened)
        token = await create_magic_link("owner@example.com")

        r = client.post("/api/auth/magic-link/consume", json={"token": token})

        assert r.status_code == 500
        assert await self._last_visit() is None

    @pytest.mark.parametrize(
        "failure",
        [
            OperationalError("UPDATE user", {}, Exception("database is locked")),
            # aiosqlite's own error for a connection closed under it, which
            # SQLAlchemy does not wrap.
            ValueError("no active connection"),
        ],
    )
    async def test_a_failed_write_is_logged_rather_than_failing_the_request(self, monkeypatch, caplog, failure):
        stale = datetime.now(UTC) - timedelta(hours=1)
        user = users.User(id=uuid.uuid4(), email="owner@example.com", is_active=True, last_seen_at=stale)

        def failing():
            raise failure

        monkeypatch.setattr(users, "async_session_maker", failing)

        await users.record_visit(user)

        assert user.last_seen_at == stale
        assert "Could not record a visit" in caplog.text
