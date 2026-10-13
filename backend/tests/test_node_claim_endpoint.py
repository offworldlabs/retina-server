"""`/v1/nodes/claim`, through the app.

The store's own behaviour is pinned in test_node_claim_store.py and the
validator's in test_node_claim_validation.py. What is tested here is what only
the route can get wrong: the order of authentication and validation, which calls
mail and which do not, the collision on an owned node, the commit, and the
answer a node gets when no mail can leave.

Mail goes through the outbox, and nothing runs its worker here: a test that
wants a mail to have gone drains the queue itself (tests/claim_mail_helpers.py),
as the worker would a moment after the answer. A nomination that replaces one
whose mail is still queued cancels it, so a test that means "the first mail
went" sends it before the second request.

Assertions read the database through a fresh query after the request rather than
off objects the handler left attached: the handler runs on its own loop and
thread, per node_client's docstring.
"""

import logging
import sqlite3

import pytest
from sqlalchemy import delete, select, text

from core.nodes import Node, NodeClaim, NodeClaimChallenge
from core.rate_counters import RateCounter
from routes.node_responses import MAIL_UNAVAILABLE_RETRY_AFTER_S
from services import claim_links, mail_outbox, node_rate_limits
from services.node_rate_limits import CLAIM_LIMITS, ClaimRateLimiter
from tests.claim_mail_helpers import HOST_APP, Outbox, configure_mail, links_in, token_in

ADA = "ada@example.com"
GRACE = "grace@example.com"


@pytest.fixture(autouse=True)
async def outbox(monkeypatch, tmp_path, node_session) -> Outbox:
    """Mail configured, one database for the claims, the links and the queue,
    and a fresh allowance.

    The claim limiter counts in rate_counters, so the allowance is whatever
    that table holds. The database is copied fresh for every test, and the
    table is emptied anyway so that no test rests on how the copy is made.
    """
    configure_mail(monkeypatch, tmp_path)
    await node_session.execute(delete(RateCounter))
    await node_session.commit()
    return Outbox(monkeypatch, node_session)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _claim(session, node_id: str) -> NodeClaim | None:
    session.expire_all()
    return (await session.execute(select(NodeClaim).where(NodeClaim.node_id == node_id))).scalars().first()


async def _challenge(session, node_id: str) -> NodeClaimChallenge | None:
    session.expire_all()
    result = await session.execute(select(NodeClaimChallenge).where(NodeClaimChallenge.node_id == node_id))
    return result.scalars().first()


async def _own(session, node_id: str, user_id: str = "11111111-1111-1111-1111-111111111111") -> None:
    """Bind the node, beside whatever address is already on file."""
    claim = await session.get(NodeClaim, node_id)
    if claim is None:
        session.add(NodeClaim(node_id=node_id, user_id=user_id, verified=False, undeliverable=False))
    else:
        claim.user_id = user_id
    await session.commit()


async def _statuses(outbox: Outbox) -> list[tuple[str, str]]:
    return [(row.recipient, row.status) for row in await outbox.rows()]


# Everything a claim writes, table by table.
_WRITTEN = ("node_claims", "node_claim_challenges", "magic_links", "mail_outbox", "rate_counters")


async def _written(outbox: Outbox) -> dict[str, list[tuple]]:
    async with outbox.sessions() as session:
        return {
            table: [tuple(row) for row in (await session.execute(text(f"SELECT * FROM {table} ORDER BY 1"))).all()]
            for table in _WRITTEN
        }


async def _links_to(outbox: Outbox, email: str) -> int:
    async with outbox.sessions() as session:
        return (await session.execute(text("SELECT COUNT(*) FROM magic_links WHERE email = :e"), {"e": email})).scalar()


# ── Nominating ───────────────────────────────────────────────────────────────


async def test_a_nomination_stores_the_address_and_answers_pending(registered_node, node_session, node_client):
    token, node_id = registered_node

    response = node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    assert response.status_code == 200
    assert response.json() == {"state": "pending", "email": ADA, "undeliverable": False}
    row = await _claim(node_session, node_id)
    assert (row.email, row.verified) == (ADA, False)


async def test_a_nomination_records_a_challenge_and_mails_its_token(registered_node, node_session, node_client, outbox):
    token, node_id = registered_node

    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    challenge = await _challenge(node_session, node_id)
    assert challenge.email == ADA
    [message] = await outbox.send()
    assert message.to == ADA
    # Only the hash is stored, so the token exists in the mail and nowhere else.
    sent_token = token_in(message)
    assert challenge.handle == claim_links.handle_for(sent_token)
    assert sent_token not in challenge.handle


async def test_the_address_is_normalised_before_it_is_stored(registered_node, node_session, node_client):
    token, node_id = registered_node

    body = node_client.put("/v1/nodes/claim", json={"email": "  Ada@Example.COM "}, headers=_auth(token)).json()

    assert body["email"] == ADA
    assert (await _claim(node_session, node_id)).email == ADA


async def test_re_sending_the_same_address_answers_pending_without_mailing_again(registered_node, node_client, outbox):
    """A write that mailed every time it ran would mail on every configuration sync."""
    token, _node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    response = node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    assert response.json() == {"state": "pending", "email": ADA, "undeliverable": False}
    assert await _statuses(outbox) == [(ADA, "queued")]


async def test_an_expired_challenge_does_not_make_a_repeated_put_mail_again(
    registered_node, node_session, node_client, outbox
):
    """A challenge lasts fifteen minutes and a node resends its configuration
    for years. If an expiry made an ordinary write mail again, one address that
    nobody clicked would be mailed on every sync until the rate limit bit."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await outbox.send()
    challenge = await _challenge(node_session, node_id)
    challenge.expires_at = 0.0
    await node_session.commit()

    response = node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    assert response.json() == {"state": "unclaimed", "email": ADA, "undeliverable": False}
    assert await _statuses(outbox) == [(ADA, "sent")]


async def test_an_expired_challenge_is_still_resendable(registered_node, node_session, node_client, outbox):
    """The explicit ask is how a fresh link is got, which is the whole reason a
    repeated write does not do it."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await outbox.send()
    challenge = await _challenge(node_session, node_id)
    challenge.expires_at = 0.0
    await node_session.commit()

    response = node_client.post("/v1/nodes/claim/resend", headers=_auth(token))
    await outbox.send()

    assert response.json() == {"state": "pending", "email": ADA, "undeliverable": False}
    assert outbox.recipients() == [ADA, ADA]


async def test_a_second_address_replaces_the_first_challenge(registered_node, node_session, node_client, outbox):
    """Only the newest link works: a link mailed to a mistyped address stops
    working the moment the address is corrected."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    [first] = await outbox.send()
    first_handle = (await _challenge(node_session, node_id)).handle

    node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))
    await outbox.send()

    challenge = await _challenge(node_session, node_id)
    assert challenge.email == GRACE
    assert challenge.handle != first_handle
    assert outbox.recipients() == [ADA, GRACE]
    assert await claim_links.peek(token_in(first)) is None


async def test_an_address_at_its_link_cap_leaves_no_dead_challenge_behind(
    registered_node, node_session, node_client, outbox
):
    """The correction kills the link it displaced even when the new address may
    not be sent another. The node must then read `unclaimed`, not `pending` for
    a link nobody can click."""
    from core.auth import _MAX_OUTSTANDING_MAGIC_LINKS, create_magic_link

    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    [first] = await outbox.send()
    for _ in range(_MAX_OUTSTANDING_MAGIC_LINKS):
        await create_magic_link(GRACE, intent=claim_links.INTENT_CLAIM, node_id="retdeadbeef")

    r = node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert r.json()["state"] == "unclaimed"
    assert await _challenge(node_session, node_id) is None
    assert node_client.get("/v1/nodes/claim", headers=_auth(token)).json()["state"] == "unclaimed"
    assert await claim_links.peek(token_in(first)) is None
    assert await outbox.send() == []
    assert outbox.recipients() == [ADA]


async def test_an_address_at_its_link_cap_cancels_the_displaced_links_queued_mail(registered_node, node_client, outbox):
    """The displaced link is dead, so a mail still waiting to carry it must not go."""
    from core.auth import _MAX_OUTSTANDING_MAGIC_LINKS, create_magic_link

    token, _node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    for _ in range(_MAX_OUTSTANDING_MAGIC_LINKS):
        await create_magic_link(GRACE, intent=claim_links.INTENT_CLAIM, node_id="retdeadbeef")

    node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert await _statuses(outbox) == [(ADA, "cancelled")]
    assert await outbox.send() == []
    assert await _links_to(outbox, ADA) == 0


async def test_a_second_address_drops_the_verified_flag(registered_node, node_session, node_client):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    row = await _claim(node_session, node_id)
    row.verified = True
    await node_session.commit()

    node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert (await _claim(node_session, node_id)).verified is False


# ── The mail ─────────────────────────────────────────────────────────────────


async def test_a_nomination_queues_one_claim_mail_carrying_the_consoles_claim_link(
    registered_node, node_session, node_client, outbox
):
    """HOST_APP serves the console at `/`, and its claim page is /auth/claim/<token>."""
    token, node_id = registered_node

    response = node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    assert response.json() == {"state": "pending", "email": ADA, "undeliverable": False}
    challenge = await _challenge(node_session, node_id)
    [row] = await outbox.rows()
    assert (row.kind, row.status, row.recipient, row.node_id) == ("claim", "queued", ADA, node_id)
    # The handle and the expiry are the challenge's: how a settlement finds its
    # claim, and when the mail stops being worth sending.
    assert (row.handle, row.expires_at) == (challenge.handle, challenge.expires_at)
    queued_body = row.body

    [message] = await outbox.send()

    sent_token = token_in(message)
    assert links_in(message) == {("https", HOST_APP, sent_token)}
    assert f"https://{HOST_APP}/auth/claim/{sent_token}" in message.text
    assert f'href="https://{HOST_APP}/auth/claim/{sent_token}"' in message.html
    assert "/dash/" not in message.text + message.html
    assert claim_links.handle_for(sent_token) == challenge.handle
    # Ciphertext while it waited: the token was in the mail and nowhere else.
    assert sent_token not in queued_body
    node_ref = (await node_session.execute(select(Node.node_ref).where(Node.node_id == node_id))).scalar_one()
    assert node_ref in message.subject
    assert node_id not in message.subject + message.text + message.html
    # Sent once: nothing is left to send, and the row says it went.
    assert await outbox.send() == []
    [row] = await outbox.rows()
    assert (row.status, row.body) == ("sent", None)


async def test_the_claim_link_follows_force_https(registered_node, node_client, outbox, monkeypatch):
    monkeypatch.setenv("FORCE_HTTPS", "false")
    token, _node_id = registered_node

    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    [message] = await outbox.send()
    assert links_in(message) == {("http", HOST_APP, token_in(message))}


async def test_a_renomination_cancels_the_mail_still_queued_for_the_first(registered_node, node_client, outbox):
    """The first link dies with the correction, so the mail carrying it must not go."""
    token, _node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert await _statuses(outbox) == [(ADA, "cancelled"), (GRACE, "queued")]
    assert [message.to for message in await outbox.send()] == [GRACE]
    assert await _links_to(outbox, ADA) == 0


async def test_a_resend_queues_a_fresh_mail_and_cancels_the_one_still_queued(
    registered_node, node_session, node_client, outbox
):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    response = node_client.post("/v1/nodes/claim/resend", headers=_auth(token))

    assert response.json() == {"state": "pending", "email": ADA, "undeliverable": False}
    first, second = await outbox.rows()
    assert (first.status, second.status) == ("cancelled", "queued")
    assert second.handle != first.handle
    assert second.handle == (await _challenge(node_session, node_id)).handle
    [message] = await outbox.send()
    assert claim_links.handle_for(token_in(message)) == second.handle
    # One live claim link for the address: the cancelled mail's died with it.
    assert await _links_to(outbox, ADA) == 1


async def test_the_worker_is_woken_only_once_the_mail_is_committed(
    registered_node, node_session, node_client, monkeypatch
):
    """A wake before the commit would have the worker look before the row is there to find."""
    database = node_session.bind.url.database
    seen: list[int] = []

    def _wake() -> None:
        connection = sqlite3.connect(database)
        try:
            seen.append(connection.execute("SELECT COUNT(*) FROM mail_outbox WHERE status = 'queued'").fetchone()[0])
        finally:
            connection.close()

    monkeypatch.setattr(mail_outbox, "wake", _wake)
    token, _node_id = registered_node

    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    assert seen == [1]


# ── When no mail can leave ───────────────────────────────────────────────────


def _no_transport(monkeypatch, _tmp_path) -> None:
    monkeypatch.delenv("MAIL_TRANSPORT")


def _no_host(monkeypatch, _tmp_path) -> None:
    monkeypatch.delenv("HOST_APP")


def _unusable_key(monkeypatch, tmp_path) -> None:
    (tmp_path / mail_outbox.KEY_FILE_NAME).write_bytes(b"not a fernet key")
    # Read again rather than served from what an earlier call loaded.
    monkeypatch.setattr(mail_outbox, "_fernets", {})


_CAUSES = {
    "no transport": (_no_transport, "mail is not configured"),
    "no HOST_APP": (_no_host, "HOST_APP is not set"),
    "an unusable outbox key": (_unusable_key, "the outbox key is unusable"),
}


def _assert_mail_unavailable(response) -> None:
    assert response.status_code == 503
    assert response.json() == {"error": "mail_unavailable"}
    assert response.headers["Retry-After"] == str(MAIL_UNAVAILABLE_RETRY_AFTER_S)


@pytest.mark.parametrize("cause", _CAUSES)
async def test_a_nomination_no_mail_can_leave_for_is_503_and_writes_nothing(
    cause, registered_node, node_client, outbox, monkeypatch, tmp_path, caplog
):
    """`pending` would have somebody watch an inbox nothing is coming to. No
    address, no challenge, no link, no queued mail, and no allowance spent."""
    caplog.set_level(logging.WARNING, logger="routes.node_claim")
    break_mail, reason = _CAUSES[cause]
    break_mail(monkeypatch, tmp_path)
    token, node_id = registered_node

    response = node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    _assert_mail_unavailable(response)
    assert await _written(outbox) == dict.fromkeys(_WRITTEN, [])
    assert node_client.get("/v1/nodes/claim", headers=_auth(token)).json() == {
        "state": "unclaimed",
        "email": None,
        "undeliverable": False,
    }
    # Says which node and why, and nothing about a link: none was minted.
    assert f"no claim mail can be sent for {node_id} ({reason})" in caplog.text


@pytest.mark.parametrize("cause", _CAUSES)
async def test_a_correction_no_mail_can_leave_for_leaves_the_pending_claim_as_it_was(
    cause, registered_node, node_client, outbox, monkeypatch, tmp_path
):
    token, _node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    before = await _written(outbox)
    break_mail, _reason = _CAUSES[cause]
    break_mail(monkeypatch, tmp_path)

    response = node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    _assert_mail_unavailable(response)
    assert await _written(outbox) == before
    assert node_client.get("/v1/nodes/claim", headers=_auth(token)).json()["email"] == ADA


@pytest.mark.parametrize("cause", _CAUSES)
async def test_a_resend_no_mail_can_leave_for_is_503_and_changes_nothing(
    cause, registered_node, node_client, outbox, monkeypatch, tmp_path
):
    """The link already sent keeps working: nothing replaced it."""
    token, _node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    [first] = await outbox.send()
    before = await _written(outbox)
    break_mail, _reason = _CAUSES[cause]
    break_mail(monkeypatch, tmp_path)

    response = node_client.post("/v1/nodes/claim/resend", headers=_auth(token))

    _assert_mail_unavailable(response)
    assert await _written(outbox) == before
    assert await claim_links.peek(token_in(first)) is not None


async def test_the_paths_that_mail_nothing_answer_as_before_with_no_mail_to_send(
    registered_node, node_session, node_client, outbox, monkeypatch
):
    """Only a request that would mail is refused. Repeating the address on
    file, an owned node, a resend with nothing on file and a resend to a bounced
    address answer where the claim stands, as they always have."""
    token, node_id = registered_node
    assert node_client.post("/v1/nodes/claim/resend", headers=_auth(token)).json()["state"] == "unclaimed"
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    monkeypatch.delenv("MAIL_TRANSPORT")

    assert node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token)).json()["state"] == "pending"

    from services.node_claim_store import mark_undeliverable

    await mark_undeliverable(node_session, node_id, ADA)
    await node_session.commit()
    bounced = node_client.post("/v1/nodes/claim/resend", headers=_auth(token))
    assert (bounced.status_code, bounced.json()["undeliverable"]) == (200, True)

    await _own(node_session, node_id)
    assert node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token)).status_code == 200
    assert node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token)).status_code == 409
    assert node_client.post("/v1/nodes/claim/resend", headers=_auth(token)).status_code == 409


@pytest.mark.parametrize("cause", ["no HOST_APP", "an unusable outbox key"])
async def test_mail_that_stops_being_possible_after_the_check_rolls_the_claim_back(
    cause, registered_node, node_client, outbox, monkeypatch, tmp_path
):
    """Configuration that changes between the check and the queueing: the
    transaction rolls back whole, the link minted for it dies, and the claim
    stands as it did, the link it would have displaced included."""
    token, _node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    [first] = await outbox.send()
    before = await _written(outbox)
    monkeypatch.setattr(mail_outbox, "unavailable", lambda: None)
    break_mail, _reason = _CAUSES[cause]
    break_mail(monkeypatch, tmp_path)

    response = node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    _assert_mail_unavailable(response)
    after = await _written(outbox)
    # The allowance was spent before the configuration was found wanting.
    assert {table: rows for table, rows in after.items() if table != "rate_counters"} == {
        table: rows for table, rows in before.items() if table != "rate_counters"
    }
    assert await _links_to(outbox, GRACE) == 0
    assert await claim_links.peek(token_in(first)) is not None
    assert node_client.get("/v1/nodes/claim", headers=_auth(token)).json()["state"] == "pending"


# ── An owned node ────────────────────────────────────────────────────────────


async def test_an_owned_node_accepts_the_address_it_is_bound_to(registered_node, node_session, node_client, outbox):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await _own(node_session, node_id)

    response = node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    assert response.status_code == 200
    assert response.json() == {"state": "owned", "email": ADA, "undeliverable": False}
    assert len(await outbox.rows()) == 1


async def test_an_owned_node_refuses_a_different_address_and_names_the_bound_one(
    registered_node, node_session, node_client, outbox
):
    """The node reconciles from the refusal rather than from a second call, which
    is what makes acting on state up to a beat old harmless."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await _own(node_session, node_id)

    response = node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert response.status_code == 409
    assert response.json() == {"state": "owned", "email": ADA, "undeliverable": False}
    assert len(await outbox.rows()) == 1


async def test_a_refused_nomination_on_an_owned_node_writes_nothing(registered_node, node_session, node_client):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await _own(node_session, node_id)

    node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert (await _claim(node_session, node_id)).email == ADA


# ── Reading ──────────────────────────────────────────────────────────────────


async def test_get_answers_unclaimed_for_a_node_that_never_nominated(registered_node, node_client):
    token, _node_id = registered_node

    response = node_client.get("/v1/nodes/claim", headers=_auth(token))

    assert response.status_code == 200
    assert response.json() == {"state": "unclaimed", "email": None, "undeliverable": False}


async def test_get_answers_owned_with_the_bound_address(registered_node, node_session, node_client):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await _own(node_session, node_id)

    assert node_client.get("/v1/nodes/claim", headers=_auth(token)).json()["state"] == "owned"


async def test_an_expired_challenge_reads_unclaimed_with_the_address_still_on_file(
    registered_node, node_session, node_client
):
    """The address stays so a fresh challenge can be asked for without retyping it."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    challenge = await _challenge(node_session, node_id)
    challenge.expires_at = 0.0
    await node_session.commit()

    assert node_client.get("/v1/nodes/claim", headers=_auth(token)).json() == {
        "state": "unclaimed",
        "email": ADA,
        "undeliverable": False,
    }


async def test_get_writes_nothing(registered_node, node_session, node_client):
    token, node_id = registered_node

    node_client.get("/v1/nodes/claim", headers=_auth(token))

    assert await _claim(node_session, node_id) is None
    assert await _challenge(node_session, node_id) is None


# ── A bounced address ────────────────────────────────────────────────────────


async def test_a_bounced_address_reads_unclaimed_with_the_flag(registered_node, node_session, node_client):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    from services.node_claim_store import mark_undeliverable

    await mark_undeliverable(node_session, node_id, ADA)
    await node_session.commit()

    assert node_client.get("/v1/nodes/claim", headers=_auth(token)).json() == {
        "state": "unclaimed",
        "email": ADA,
        "undeliverable": True,
    }


async def test_a_bounced_address_is_not_mailed_again(registered_node, node_session, node_client, outbox):
    """A second copy to an address that does not exist earns a second bounce, and
    bounces are how a sending domain loses its reputation."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await outbox.send()
    from services.node_claim_store import mark_undeliverable

    await mark_undeliverable(node_session, node_id, ADA)
    await node_session.commit()

    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    node_client.post("/v1/nodes/claim/resend", headers=_auth(token))

    assert await outbox.send() == []
    assert outbox.recipients() == [ADA]


async def test_a_different_address_after_a_bounce_is_mailed_normally(
    registered_node, node_session, node_client, outbox
):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await outbox.send()
    from services.node_claim_store import mark_undeliverable

    await mark_undeliverable(node_session, node_id, ADA)
    await node_session.commit()

    response = node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))
    await outbox.send()

    assert response.json() == {"state": "pending", "email": GRACE, "undeliverable": False}
    assert outbox.recipients() == [ADA, GRACE]


# ── Resending ────────────────────────────────────────────────────────────────


async def test_resend_mails_again_where_a_repeated_put_does_not(registered_node, node_client, outbox):
    token, _node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await outbox.send()

    response = node_client.post("/v1/nodes/claim/resend", headers=_auth(token))
    await outbox.send()

    assert response.json() == {"state": "pending", "email": ADA, "undeliverable": False}
    assert outbox.recipients() == [ADA, ADA]


async def test_resend_mints_a_fresh_token_rather_than_repeating_the_old_one(
    registered_node, node_session, node_client, outbox
):
    """The link is what expires, so a second copy of the old token would mail
    somebody a link with less life left than the one they did not get."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    [first] = await outbox.send()
    first_handle = (await _challenge(node_session, node_id)).handle

    node_client.post("/v1/nodes/claim/resend", headers=_auth(token))
    [second] = await outbox.send()

    assert (await _challenge(node_session, node_id)).handle != first_handle
    assert token_in(first) != token_in(second)
    # Only the newest works.
    assert await claim_links.peek(token_in(first)) is None
    assert await claim_links.peek(token_in(second)) is not None


async def test_resend_with_nothing_on_file_answers_unclaimed_and_mails_nothing(registered_node, node_client, outbox):
    token, _node_id = registered_node

    response = node_client.post("/v1/nodes/claim/resend", headers=_auth(token))

    assert response.json() == {"state": "unclaimed", "email": None, "undeliverable": False}
    assert await outbox.rows() == []


async def test_resend_on_an_owned_node_is_refused(registered_node, node_session, node_client, outbox):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await _own(node_session, node_id)

    response = node_client.post("/v1/nodes/claim/resend", headers=_auth(token))

    assert response.status_code == 409
    assert len(await outbox.rows()) == 1


# ── Refusals ─────────────────────────────────────────────────────────────────


async def test_no_bearer_is_401_and_writes_nothing(registered_node, node_session, node_client):
    _token, node_id = registered_node

    response = node_client.put("/v1/nodes/claim", json={"email": ADA})

    assert response.status_code == 401
    assert await _claim(node_session, node_id) is None


async def test_a_bad_bearer_is_401_rather_than_a_body_refusal(registered_node, node_client):
    """Identity resolves before the body, so a broken body behind a bad token is a 401."""
    response = node_client.put("/v1/nodes/claim", json={"code": "X"}, headers=_auth("not-a-token"))

    assert response.status_code == 401


async def test_a_get_without_a_bearer_is_401(registered_node, node_client):
    assert node_client.get("/v1/nodes/claim").status_code == 401


async def test_a_missing_address_is_400_invalid_claim(registered_node, node_client, outbox):
    token, _node_id = registered_node

    response = node_client.put("/v1/nodes/claim", json={}, headers=_auth(token))

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_claim", "detail": "email"}
    assert await outbox.rows() == []


async def test_an_address_that_is_not_one_is_400(registered_node, node_client, outbox):
    token, _node_id = registered_node

    response = node_client.put("/v1/nodes/claim", json={"email": "ada"}, headers=_auth(token))

    assert response.status_code == 400
    assert await outbox.rows() == []


async def test_a_body_that_is_not_json_is_400_invalid_claim(registered_node, node_client):
    token, _node_id = registered_node

    response = node_client.put(
        "/v1/nodes/claim",
        content=b"not json",
        headers={**_auth(token), "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_claim"


async def test_the_sixth_nomination_in_an_hour_is_429_with_a_retry_after(
    registered_node, node_session, node_client, outbox
):
    """Counted in rate_counters, so another worker, or this one after a restart,
    spends the same allowance rather than a fresh one."""
    token, node_id = registered_node
    for i in range(5):
        assert node_client.put(
            "/v1/nodes/claim", json={"email": f"ada{i}@example.com"}, headers=_auth(token)
        ).is_success

    response = node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert response.status_code == 429
    assert response.json() == {"error": "rate_limited"}
    assert 1 <= int(response.headers["Retry-After"]) <= 3600
    assert len(await outbox.rows()) == 5
    assert await _links_to(outbox, GRACE) == 0
    node_key = node_rate_limits._counter_key("claim", node_id)
    async with outbox.sessions() as session:
        counts = dict(
            (
                await session.execute(
                    select(RateCounter.window_s, RateCounter.count).where(RateCounter.key == node_key)
                )
            ).all()
        )
        assert counts == {window_s: 5 for _limit, window_s in CLAIM_LIMITS}
        # A limiter holding nothing in memory, as another process's would, is
        # held to the same count.
        refusal = await ClaimRateLimiter().admit(session, node_id, "someone-new@example.com")
    assert refusal is not None and refusal.status_code == 429


async def test_a_rate_limited_nomination_is_not_stored(registered_node, node_session, node_client):
    token, node_id = registered_node
    for i in range(5):
        node_client.put("/v1/nodes/claim", json={"email": f"ada{i}@example.com"}, headers=_auth(token))

    node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert (await _claim(node_session, node_id)).email == "ada4@example.com"
