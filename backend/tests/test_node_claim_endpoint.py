"""`/v1/nodes/claim`, through the app.

The store's own behaviour is pinned in test_node_claim_store.py and the
validator's in test_node_claim_validation.py. What is tested here is what only
the route can get wrong: the order of authentication and validation, which calls
mail and which do not, the collision on an owned node, and the commit.

Assertions read the database through a fresh query after the request rather than
off objects the handler left attached: the handler runs on its own loop and
thread, per node_client's docstring.
"""

import pytest
from sqlalchemy import select

from core.nodes import NodeClaim, NodeClaimChallenge
from core.users import NodeOwner
from services import claim_links
from services.node_rate_limits import claim_rate_limiter

ADA = "ada@example.com"
GRACE = "grace@example.com"


@pytest.fixture(autouse=True)
def _reset_claim_limiter():
    """The limiter is a module-level singleton, so counters otherwise carry
    across tests and a suite run in one order refuses what another admits."""
    claim_rate_limiter.reset()
    yield
    claim_rate_limiter.reset()


@pytest.fixture
def delivered(monkeypatch):
    """Every address a link was sent to, in order.

    The real deliver() has no transport on a test box and answers False, which
    is indistinguishable from never being called; a test asserting "no mail was
    sent" has to be able to tell those apart.
    """
    sent: list[tuple[str, str]] = []

    async def _deliver(email: str, node_ref: str, token: str) -> bool:
        sent.append((email, token))
        return True

    monkeypatch.setattr(claim_links, "deliver", _deliver)
    return sent


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
    session.add(NodeOwner(node_id=node_id, user_id=user_id))
    await session.commit()


# ── Nominating ───────────────────────────────────────────────────────────────


async def test_a_nomination_stores_the_address_and_answers_pending(
    registered_node, node_session, node_client, delivered
):
    token, node_id = registered_node

    response = node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    assert response.status_code == 200
    assert response.json() == {"state": "pending", "email": ADA, "undeliverable": False}
    row = await _claim(node_session, node_id)
    assert (row.email, row.verified) == (ADA, False)


async def test_a_nomination_records_a_challenge_and_mails_its_token(
    registered_node, node_session, node_client, delivered
):
    token, node_id = registered_node

    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    challenge = await _challenge(node_session, node_id)
    assert challenge.email == ADA
    assert [address for address, _token in delivered] == [ADA]
    # Only the hash is stored, so the token exists in the mail and nowhere else.
    sent_token = delivered[0][1]
    assert challenge.handle == claim_links.handle_for(sent_token)
    assert sent_token not in challenge.handle


async def test_the_address_is_normalised_before_it_is_stored(registered_node, node_session, node_client, delivered):
    token, node_id = registered_node

    body = node_client.put("/v1/nodes/claim", json={"email": "  Ada@Example.COM "}, headers=_auth(token)).json()

    assert body["email"] == ADA
    assert (await _claim(node_session, node_id)).email == ADA


async def test_re_sending_the_same_address_answers_pending_without_mailing_again(
    registered_node, node_client, delivered
):
    """A write that mailed every time it ran would mail on every configuration sync."""
    token, _node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    response = node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    assert response.json() == {"state": "pending", "email": ADA, "undeliverable": False}
    assert len(delivered) == 1


async def test_an_expired_challenge_does_not_make_a_repeated_put_mail_again(
    registered_node, node_session, node_client, delivered
):
    """A challenge lasts fifteen minutes and a node resends its configuration
    for years. If an expiry made an ordinary write mail again, one address that
    nobody clicked would be mailed on every sync until the rate limit bit."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    challenge = await _challenge(node_session, node_id)
    challenge.expires_at = 0.0
    await node_session.commit()

    response = node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    assert len(delivered) == 1
    assert response.json() == {"state": "unclaimed", "email": ADA, "undeliverable": False}


async def test_an_expired_challenge_is_still_resendable(registered_node, node_session, node_client, delivered):
    """The explicit ask is how a fresh link is got, which is the whole reason a
    repeated write does not do it."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    challenge = await _challenge(node_session, node_id)
    challenge.expires_at = 0.0
    await node_session.commit()

    response = node_client.post("/v1/nodes/claim/resend", headers=_auth(token))

    assert response.json() == {"state": "pending", "email": ADA, "undeliverable": False}
    assert [address for address, _token in delivered] == [ADA, ADA]


async def test_a_second_address_replaces_the_first_challenge(registered_node, node_session, node_client, delivered):
    """Only the newest link works: a link mailed to a mistyped address stops
    working the moment the address is corrected."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    first_handle = (await _challenge(node_session, node_id)).handle

    node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    challenge = await _challenge(node_session, node_id)
    assert challenge.email == GRACE
    assert challenge.handle != first_handle
    assert [address for address, _token in delivered] == [ADA, GRACE]


async def test_an_address_at_its_link_cap_leaves_no_dead_challenge_behind(
    registered_node, node_session, node_client, delivered
):
    """The correction kills the link it displaced even when the new address may
    not be sent another. The node must then read `unclaimed`, not `pending` for
    a link nobody can click."""
    from core.auth import _MAX_OUTSTANDING_MAGIC_LINKS, create_magic_link

    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    for _ in range(_MAX_OUTSTANDING_MAGIC_LINKS):
        await create_magic_link(GRACE, intent=claim_links.INTENT_CLAIM, node_id="retdeadbeef")

    r = node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert r.json()["state"] == "unclaimed"
    assert await _challenge(node_session, node_id) is None
    assert node_client.get("/v1/nodes/claim", headers=_auth(token)).json()["state"] == "unclaimed"
    assert [address for address, _token in delivered] == [ADA]


async def test_a_second_address_drops_the_verified_flag(registered_node, node_session, node_client, delivered):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    row = await _claim(node_session, node_id)
    row.verified = True
    await node_session.commit()

    node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert (await _claim(node_session, node_id)).verified is False


# ── An owned node ────────────────────────────────────────────────────────────


async def test_an_owned_node_accepts_the_address_it_is_bound_to(registered_node, node_session, node_client, delivered):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await _own(node_session, node_id)

    response = node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    assert response.status_code == 200
    assert response.json() == {"state": "owned", "email": ADA, "undeliverable": False}
    assert len(delivered) == 1


async def test_an_owned_node_refuses_a_different_address_and_names_the_bound_one(
    registered_node, node_session, node_client, delivered
):
    """The node reconciles from the refusal rather than from a second call, which
    is what makes acting on state up to a beat old harmless."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await _own(node_session, node_id)

    response = node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert response.status_code == 409
    assert response.json() == {"state": "owned", "email": ADA, "undeliverable": False}
    assert len(delivered) == 1


async def test_a_refused_nomination_on_an_owned_node_writes_nothing(
    registered_node, node_session, node_client, delivered
):
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


async def test_get_answers_owned_with_the_bound_address(registered_node, node_session, node_client, delivered):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await _own(node_session, node_id)

    assert node_client.get("/v1/nodes/claim", headers=_auth(token)).json()["state"] == "owned"


async def test_an_expired_challenge_reads_unclaimed_with_the_address_still_on_file(
    registered_node, node_session, node_client, delivered
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


async def test_a_bounced_address_reads_unclaimed_with_the_flag(registered_node, node_session, node_client, delivered):
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


async def test_a_bounced_address_is_not_mailed_again(registered_node, node_session, node_client, delivered):
    """A second copy to an address that does not exist earns a second bounce, and
    bounces are how a sending domain loses its reputation."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    from services.node_claim_store import mark_undeliverable

    await mark_undeliverable(node_session, node_id, ADA)
    await node_session.commit()

    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    node_client.post("/v1/nodes/claim/resend", headers=_auth(token))

    assert len(delivered) == 1


async def test_a_different_address_after_a_bounce_is_mailed_normally(
    registered_node, node_session, node_client, delivered
):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    from services.node_claim_store import mark_undeliverable

    await mark_undeliverable(node_session, node_id, ADA)
    await node_session.commit()

    response = node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert response.json() == {"state": "pending", "email": GRACE, "undeliverable": False}
    assert [address for address, _token in delivered] == [ADA, GRACE]


# ── Resending ────────────────────────────────────────────────────────────────


async def test_resend_mails_again_where_a_repeated_put_does_not(registered_node, node_client, delivered):
    token, _node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))

    response = node_client.post("/v1/nodes/claim/resend", headers=_auth(token))

    assert response.json() == {"state": "pending", "email": ADA, "undeliverable": False}
    assert [address for address, _token in delivered] == [ADA, ADA]


async def test_resend_mints_a_fresh_token_rather_than_repeating_the_old_one(
    registered_node, node_session, node_client, delivered
):
    """The link is what expires, so a second copy of the old token would mail
    somebody a link with less life left than the one they did not get."""
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    first_handle = (await _challenge(node_session, node_id)).handle

    node_client.post("/v1/nodes/claim/resend", headers=_auth(token))

    assert (await _challenge(node_session, node_id)).handle != first_handle
    assert delivered[0][1] != delivered[1][1]


async def test_resend_with_nothing_on_file_answers_unclaimed_and_mails_nothing(registered_node, node_client, delivered):
    token, _node_id = registered_node

    response = node_client.post("/v1/nodes/claim/resend", headers=_auth(token))

    assert response.json() == {"state": "unclaimed", "email": None, "undeliverable": False}
    assert delivered == []


async def test_resend_on_an_owned_node_is_refused(registered_node, node_session, node_client, delivered):
    token, node_id = registered_node
    node_client.put("/v1/nodes/claim", json={"email": ADA}, headers=_auth(token))
    await _own(node_session, node_id)

    response = node_client.post("/v1/nodes/claim/resend", headers=_auth(token))

    assert response.status_code == 409
    assert len(delivered) == 1


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


async def test_a_missing_address_is_400_invalid_claim(registered_node, node_client, delivered):
    token, _node_id = registered_node

    response = node_client.put("/v1/nodes/claim", json={}, headers=_auth(token))

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_claim", "detail": "email"}
    assert delivered == []


async def test_an_address_that_is_not_one_is_400(registered_node, node_client, delivered):
    token, _node_id = registered_node

    response = node_client.put("/v1/nodes/claim", json={"email": "ada"}, headers=_auth(token))

    assert response.status_code == 400
    assert delivered == []


async def test_a_body_that_is_not_json_is_400_invalid_claim(registered_node, node_client):
    token, _node_id = registered_node

    response = node_client.put(
        "/v1/nodes/claim",
        content=b"not json",
        headers={**_auth(token), "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_claim"


async def test_the_sixth_nomination_in_an_hour_is_429_with_a_retry_after(registered_node, node_client, delivered):
    token, _node_id = registered_node
    for i in range(5):
        node_client.put("/v1/nodes/claim", json={"email": f"ada{i}@example.com"}, headers=_auth(token))

    response = node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert response.status_code == 429
    assert response.json() == {"error": "rate_limited"}
    assert int(response.headers["Retry-After"]) >= 1
    assert len(delivered) == 5


async def test_a_rate_limited_nomination_is_not_stored(registered_node, node_session, node_client, delivered):
    token, node_id = registered_node
    for i in range(5):
        node_client.put("/v1/nodes/claim", json={"email": f"ada{i}@example.com"}, headers=_auth(token))

    node_client.put("/v1/nodes/claim", json={"email": GRACE}, headers=_auth(token))

    assert (await _claim(node_session, node_id)).email == "ada4@example.com"
