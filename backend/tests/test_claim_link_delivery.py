"""Putting a claim link in front of the person it was minted for.

What is tested here is the mail `claim_links.queue` leaves in the outbox: the
URL it carries, which nothing a node sees can show, what the row keeps for the
settlement that follows it, and what is refused when the mail could not be
addressed or kept. tests/test_node_claim_endpoint.py drives the same mail
through the route, and tests/test_claim_mail_outcomes.py what its settlement
does.
"""

import time

import pytest

from services import claim_links, mail_outbox
from services.claim_links import CHALLENGE_EXPIRY_S, Challenge
from tests.claim_mail_helpers import HOST_APP, Outbox, configure_mail, links_in

ADA = "ada@example.com"
NODE = "ret1a2b3c4d"
NODE_REF = "nde1a2b3c4d00"
TOKEN = "a-token-only-the-mail-carries"


@pytest.fixture(autouse=True)
def outbox(monkeypatch, tmp_path, node_session) -> Outbox:
    configure_mail(monkeypatch, tmp_path)
    return Outbox(monkeypatch, node_session)


def _challenge(token: str = TOKEN) -> Challenge:
    return Challenge(handle=claim_links.handle_for(token), token=token, expires_at=time.time() + CHALLENGE_EXPIRY_S)


async def _queue(session, challenge: Challenge | None = None) -> Challenge:
    challenge = challenge or _challenge()
    await claim_links.queue(session, challenge, email=ADA, node_id=NODE, node_ref=NODE_REF)
    return challenge


async def test_the_link_lands_on_the_consoles_claim_page(node_session, outbox):
    """HOST_APP serves the console at `/`, and it has no page under /dash/."""
    await _queue(node_session)
    await node_session.commit()

    [message] = await outbox.send()

    assert message.to == ADA
    assert NODE_REF in message.subject
    assert links_in(message) == {("https", HOST_APP, TOKEN)}
    assert f"https://{HOST_APP}/auth/claim/{TOKEN}" in message.text
    assert f'href="https://{HOST_APP}/auth/claim/{TOKEN}"' in message.html
    assert "/dash/" not in message.text + message.html
    assert f"expires in {CHALLENGE_EXPIRY_S // 60} minutes" in message.text


async def test_the_link_follows_force_https(node_session, outbox, monkeypatch):
    monkeypatch.setenv("FORCE_HTTPS", "false")

    await _queue(node_session)
    await node_session.commit()

    [message] = await outbox.send()
    assert links_in(message) == {("http", HOST_APP, TOKEN)}


async def test_the_mail_names_the_node_by_its_public_handle_alone(node_session, outbox):
    await _queue(node_session)
    await node_session.commit()

    [message] = await outbox.send()

    assert NODE not in message.subject + message.text + message.html


async def test_the_row_keeps_what_its_settlement_needs_and_the_token_only_as_ciphertext(node_session, outbox):
    challenge = await _queue(node_session)
    await node_session.commit()

    [row] = await outbox.rows()

    assert (row.kind, row.status, row.recipient, row.node_id) == (mail_outbox.KIND_CLAIM, "queued", ADA, NODE)
    assert (row.handle, row.expires_at) == (challenge.handle, challenge.expires_at)
    assert NODE_REF in row.subject
    assert TOKEN not in row.body
    assert ADA not in row.body


async def test_the_mail_goes_only_if_the_caller_commits(node_session, outbox):
    """Flushed into the caller's transaction, so a challenge rolled back is a
    mail that never goes."""
    await _queue(node_session)
    await node_session.rollback()

    assert await outbox.rows() == []
    assert await outbox.send() == []


async def test_nothing_is_queued_without_a_host_to_address_the_link_to(node_session, outbox, monkeypatch):
    """The request came from a node, so there is no browser host to fall back
    on, and a Host header is not one to trust with a credential anyway."""
    monkeypatch.delenv("HOST_APP")

    with pytest.raises(claim_links.MailUnavailable) as refused:
        await _queue(node_session)
    await node_session.commit()

    assert str(refused.value) == "HOST_APP is not set"
    assert await outbox.rows() == []


async def test_nothing_is_queued_under_a_key_that_cannot_be_used(node_session, outbox, monkeypatch, tmp_path):
    """A mail that cannot be sealed is refused rather than stored where the link could be read."""
    (tmp_path / mail_outbox.KEY_FILE_NAME).write_bytes(b"not a fernet key")

    with pytest.raises(claim_links.MailUnavailable) as refused:
        await _queue(node_session)
    await node_session.commit()

    assert str(refused.value) == "the outbox key is unusable"
    assert isinstance(refused.value.__cause__, mail_outbox.OutboxKeyError)
    assert TOKEN not in str(refused.value.__cause__)
    assert await outbox.rows() == []


async def test_a_link_killed_in_a_transaction_dies_with_its_commit(node_session, outbox):
    challenge = await claim_links.issue(email=ADA, node_id=NODE, now=time.time())

    await claim_links.invalidate_in(node_session, challenge.handle)
    # Another connection still finds it: nothing is committed yet.
    assert await claim_links.peek(challenge.token) is not None
    await node_session.commit()

    assert await claim_links.peek(challenge.token) is None


async def test_a_link_killed_in_a_transaction_rolled_back_lives_on(node_session, outbox):
    challenge = await claim_links.issue(email=ADA, node_id=NODE, now=time.time())

    await claim_links.invalidate_in(node_session, challenge.handle)
    await node_session.rollback()

    assert await claim_links.peek(challenge.token) is not None
