"""A claim mail's way out, for the suites that drive it through the node routes.

`node_client` points the route's session at the per-test database, and the
route queues the mail there. The link store opens sessions of its own through
core.auth, which would reach the suite's shared database instead. Production
keeps the two in one file, and the claim flow rests on that: a settlement kills
a link in the transaction that settles its mail. `Outbox` points the link store
at the per-test database too, so the claims, the links, the queue and the rate
counters are one file, as they are on a droplet.

Mail is configured as a laptop runs it, MAIL_TRANSPORT=log, and the key is kept
beside the test's own files. Nothing here calls mail.deliver or starts a
worker: the queue is drained by hand with a fake transport, so no test waits on
a poll and none reaches the network.
"""

import re

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import core.auth
from core.mail_outbox import MailOutbox
from services import mail, mail_outbox

HOST_APP = "app.retina.fm"

_LINK = re.compile(r"(https?)://([^/\s\"]+)/auth/claim/([A-Za-z0-9_-]+)")


def configure_mail(monkeypatch: pytest.MonkeyPatch, key_dir) -> None:
    """Mail that could go: a transport, a sender, a host for the link, and a key."""
    monkeypatch.setenv("MAIL_TRANSPORT", "log")
    monkeypatch.setenv("MAIL_FROM", "RETINA <no-reply@retina.fm>")
    monkeypatch.setenv("HOST_APP", HOST_APP)
    monkeypatch.delenv("FORCE_HTTPS", raising=False)
    # Beside this test's files rather than beside the suite's shared database,
    # which every run on the machine would share.
    monkeypatch.setattr(mail_outbox, "_key_path", lambda: key_dir / mail_outbox.KEY_FILE_NAME)
    monkeypatch.setattr(mail_outbox, "_fernets", {})


def links_in(message: mail.Message) -> set[tuple[str, str, str]]:
    """Every claim link the message carries, as (scheme, host, token), from
    either part. The HTML escapes nothing a token is made of."""
    return set(_LINK.findall(message.text)) | set(_LINK.findall(message.html))


def token_in(message: mail.Message) -> str:
    """The one token the message carries, the same in the text and the HTML."""
    [(_scheme, _host, token)] = links_in(message)
    return token


class Transport:
    """Answers with `deliveries` in turn, repeating the last, and keeps what it was handed."""

    def __init__(self, *deliveries: mail.Delivery) -> None:
        self.deliveries = list(deliveries) or [mail.Delivery(mail.SENT, "accepted", message_id="msg")]
        self.sent: list[mail.Message] = []

    def __call__(self, message: mail.Message) -> mail.Delivery:
        self.sent.append(message)
        return self.deliveries.pop(0) if len(self.deliveries) > 1 else self.deliveries[0]


class Outbox:
    """The per-test database's queue, and a worker run by hand over it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> None:
        self.sessions = async_sessionmaker(session.bind, expire_on_commit=False)
        monkeypatch.setattr(core.auth, "async_session_maker", self.sessions)
        self.mailed: list[mail.Message] = []

    async def rows(self) -> list[MailOutbox]:
        async with self.sessions() as session:
            return list((await session.execute(select(MailOutbox).order_by(MailOutbox.id))).scalars())

    async def send(self, *deliveries: mail.Delivery, now: float | None = None) -> list[mail.Message]:
        """Run the worker until nothing is due, the transport answering
        `deliveries` (sent, unless told otherwise), and return what it was handed."""
        transport = Transport(*deliveries)
        while await mail_outbox.run_once(sessions=self.sessions, owner="test-worker", now=now, transport=transport):
            pass
        self.mailed.extend(transport.sent)
        return transport.sent

    def recipients(self) -> list[str]:
        return [message.to for message in self.mailed]
