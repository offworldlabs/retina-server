"""Redeeming a claim link, declining one, and releasing a node.

These sit on `/api/auth`, not under `/v1/nodes`, because the person clicking is
not the node. They reach the application's own database through
`async_session_maker` rather than an injected session, so this suite uses the
ordinary `client` fixture and the shared database the other auth suites use.

What is tested here is what only these routes can get wrong: that one click
binds, creates and signs in; that a second click on the same link does nothing;
that a link for a node somebody else has since claimed is refused rather than
silently rebinding it; and that a release leaves nothing behind for the next
owner to read.
"""

import time

import pytest
from sqlalchemy import select

from core.auth import ClaimOutcome, complete_claim, decline_claim, get_node_owner, release_node, set_node_owner
from core.nodes import Node, NodeClaim, NodeClaimChallenge
from core.users import async_session_maker
from services import claim_links
from services.node_claim_store import clear_claim, put_challenge, read_challenge, read_claim, set_claim_address

ADA = "ada@example.com"
GRACE = "grace@example.com"
NODE_ID = "ret1a2b3c4d"
NODE_REF = "nde1a2b3c4d00"


@pytest.fixture
async def claimable_node():
    """A registered node with an address offered and a live link in the post.

    Returns the token that was mailed. Built through the same calls the node
    route makes, so a change to how a challenge is recorded fails here too
    rather than leaving this suite testing a shape nothing produces.
    """
    now = time.time()
    challenge = claim_links.issue(intent=claim_links.INTENT_CLAIM, node_id=NODE_ID, now=now)
    async with async_session_maker() as session:
        async with session.begin():
            if await session.get(Node, NODE_ID) is None:
                session.add(Node(node_id=NODE_ID, node_ref=NODE_REF, board_model="raspberrypi5-4gb"))
            await set_claim_address(session, NODE_ID, ADA)
            await put_challenge(session, NODE_ID, ADA, challenge.handle, challenge.expires_at)
    return challenge.token


async def _owner() -> str | None:
    return await get_node_owner(NODE_ID)


async def _claim_row():
    async with async_session_maker() as session:
        return await read_claim(session, NODE_ID)


async def _challenge_row():
    async with async_session_maker() as session:
        return await read_challenge(session, NODE_ID)


# ── The click ────────────────────────────────────────────────────────────────


async def test_one_click_binds_the_node(claimable_node):
    outcome, user, node_ref = await complete_claim(claimable_node)

    assert outcome is ClaimOutcome.BOUND
    assert node_ref == NODE_REF
    assert await _owner() == str(user.id)


async def test_one_click_creates_the_account_when_the_address_is_new(claimable_node):
    """The first node, the first user: an account, a binding and a session from
    one click, with nothing to sign up for beforehand."""
    _outcome, user, _ref = await complete_claim(claimable_node)

    assert user.email == ADA
    assert user.is_verified is True


async def test_one_click_confirms_the_address(claimable_node):
    await complete_claim(claimable_node)

    row = await _claim_row()
    assert (row.email, row.verified) == (ADA, True)


async def test_one_click_spends_the_link(claimable_node):
    await complete_claim(claimable_node)

    assert await _challenge_row() is None


async def test_a_second_click_on_the_same_link_does_nothing(claimable_node):
    first_outcome, first_user, _ref = await complete_claim(claimable_node)

    second_outcome, second_user, _ref = await complete_claim(claimable_node)

    assert first_outcome is ClaimOutcome.BOUND
    assert second_outcome is ClaimOutcome.INVALID
    assert second_user is None
    assert await _owner() == str(first_user.id)


async def test_a_second_node_joins_the_account_the_address_already_has(claimable_node):
    """The confirmation mail on a second node is what stops a typo attaching
    hardware to somebody else's account, so it has to land on the same one."""
    _outcome, first_user, _ref = await complete_claim(claimable_node)

    second = claim_links.issue(intent=claim_links.INTENT_CLAIM, node_id="retdeadbeef", now=time.time())
    async with async_session_maker() as session:
        async with session.begin():
            session.add(Node(node_id="retdeadbeef", node_ref="ndedeadbeef00", board_model="raspberrypi5-4gb"))
            await set_claim_address(session, "retdeadbeef", ADA)
            await put_challenge(session, "retdeadbeef", ADA, second.handle, second.expires_at)

    outcome, second_user, _ref = await complete_claim(second.token)

    assert outcome is ClaimOutcome.BOUND
    assert second_user.id == first_user.id


async def test_an_expired_link_is_refused(claimable_node):
    async with async_session_maker() as session:
        async with session.begin():
            row = await session.get(NodeClaimChallenge, NODE_ID)
            row.expires_at = 0.0

    outcome, _user, _ref = await complete_claim(claimable_node)

    assert outcome is ClaimOutcome.INVALID
    assert await _owner() is None


async def test_an_unknown_link_is_refused():
    outcome, _user, _ref = await complete_claim("not-a-token")

    assert outcome is ClaimOutcome.INVALID


async def test_an_empty_token_is_refused():
    outcome, _user, _ref = await complete_claim("")

    assert outcome is ClaimOutcome.INVALID


async def test_a_click_landing_on_the_clickers_own_binding_succeeds(claimable_node):
    """Two clicks on one link, racing: the second finds the binding the first
    made. It is the same person's, so that is success, not a collision."""
    _outcome, user, _ref = await complete_claim(claimable_node)
    async with async_session_maker() as session:
        async with session.begin():
            second = claim_links.issue(intent=claim_links.INTENT_CLAIM, node_id=NODE_ID, now=time.time())
            await put_challenge(session, NODE_ID, ADA, second.handle, second.expires_at)

    outcome, again, node_ref = await complete_claim(second.token)

    assert outcome is ClaimOutcome.BOUND
    assert again.id == user.id
    assert node_ref == NODE_REF
    assert await _challenge_row() is None


async def test_a_link_for_a_node_claimed_meanwhile_is_refused(claimable_node):
    """A valid link, but the joint proof does not extend to a node that already
    has an owner: there the incumbent is the authority, not whoever holds the
    hardware. Release is the way through, not a stronger link."""
    await set_node_owner(NODE_ID, "11111111-1111-1111-1111-111111111111")

    outcome, _user, _ref = await complete_claim(claimable_node)

    assert outcome is ClaimOutcome.TAKEN
    assert await _owner() == "11111111-1111-1111-1111-111111111111"


async def test_a_refused_click_leaves_the_address_unconfirmed(claimable_node):
    await set_node_owner(NODE_ID, "11111111-1111-1111-1111-111111111111")

    await complete_claim(claimable_node)

    assert (await _claim_row()).verified is False


async def test_a_link_for_an_address_the_node_has_since_replaced_is_refused(claimable_node):
    """The state a nomination of another address leaves when it lands after the
    link was read: the link still resolves, but the node is offering somebody
    else. Binding on it would hand the node to an address its holder withdrew."""
    async with async_session_maker() as session:
        async with session.begin():
            await set_claim_address(session, NODE_ID, GRACE)

    outcome, _user, _ref = await complete_claim(claimable_node)

    assert outcome is ClaimOutcome.INVALID
    assert await _owner() is None


async def test_a_link_still_in_a_mailbox_cannot_claim_a_node_whose_claim_was_cleared(claimable_node):
    """What a release, or an administrator clearing an owner, leaves behind: no
    address on file. A link sent before that must not bind the node."""
    async with async_session_maker() as session:
        async with session.begin():
            await clear_claim(session, NODE_ID)

    outcome, _user, _ref = await complete_claim(claimable_node)

    assert outcome is ClaimOutcome.INVALID
    assert await _owner() is None


# ── Declining ────────────────────────────────────────────────────────────────


async def test_a_decline_returns_the_node_to_unowned(claimable_node):
    assert await decline_claim(claimable_node) is True

    assert await _owner() is None
    assert await _challenge_row() is None


async def test_a_decline_leaves_the_address_on_file(claimable_node):
    """Whoever is standing at the node needs to see which address was tried, or
    nothing about the silence makes sense to them."""
    await decline_claim(claimable_node)

    assert (await _claim_row()).email == ADA


async def test_a_declined_link_cannot_then_be_redeemed(claimable_node):
    await decline_claim(claimable_node)

    outcome, _user, _ref = await complete_claim(claimable_node)

    assert outcome is ClaimOutcome.INVALID


async def test_declining_an_unknown_link_is_false():
    assert await decline_claim("not-a-token") is False


# ── Releasing ────────────────────────────────────────────────────────────────


async def test_a_release_clears_the_binding_and_the_address(claimable_node):
    """A second-hand node that kept its address would show its next owner the
    last one's."""
    _outcome, user, _ref = await complete_claim(claimable_node)

    assert await release_node(NODE_ID, str(user.id)) is True

    assert await _owner() is None
    assert await _claim_row() is None


async def test_a_released_node_can_be_claimed_again(claimable_node):
    _outcome, user, _ref = await complete_claim(claimable_node)
    await release_node(NODE_ID, str(user.id))

    second = claim_links.issue(intent=claim_links.INTENT_CLAIM, node_id=NODE_ID, now=time.time())
    async with async_session_maker() as session:
        async with session.begin():
            await set_claim_address(session, NODE_ID, GRACE)
            await put_challenge(session, NODE_ID, GRACE, second.handle, second.expires_at)

    outcome, new_user, _ref = await complete_claim(second.token)

    assert outcome is ClaimOutcome.BOUND
    assert new_user.email == GRACE
    assert await _owner() == str(new_user.id)


async def test_a_release_by_somebody_who_does_not_own_it_is_refused(claimable_node):
    _outcome, user, _ref = await complete_claim(claimable_node)

    assert await release_node(NODE_ID, "11111111-1111-1111-1111-111111111111") is False

    assert await _owner() == str(user.id)


async def test_releasing_an_unowned_node_is_false():
    assert await release_node(NODE_ID, "11111111-1111-1111-1111-111111111111") is False


# ── The seam ─────────────────────────────────────────────────────────────────


async def test_a_challenge_minted_under_another_intent_cannot_claim():
    """A claim link must not work as a bare sign-in if it is forwarded, and a
    sign-in link must not claim. Both rest on this check rather than on a token
    only ever reaching the endpoint it was minted for."""
    with pytest.raises(ValueError):
        claim_links.issue(intent="signin", node_id=NODE_ID, now=time.time())


async def test_only_the_hash_of_a_token_is_ever_stored(claimable_node):
    async with async_session_maker() as session:
        rows = (await session.execute(select(NodeClaimChallenge))).scalars().all()

    assert rows
    for row in rows:
        assert claimable_node not in row.handle
        assert row.handle == claim_links.handle_for(claimable_node)


async def test_a_claim_row_survives_a_re_registration(claimable_node):
    """Registration clears the contact document. The binding and the address it
    was made with live outside it precisely so a reflash cannot take them."""
    from services.node_contact_store import delete_contact

    await complete_claim(claimable_node)
    async with async_session_maker() as session:
        async with session.begin():
            await delete_contact(session, NODE_ID)

    assert (await _claim_row()).verified is True
    assert await _owner() is not None


async def test_the_claim_row_and_the_node_row_agree_on_the_node(claimable_node):
    async with async_session_maker() as session:
        claim = (await session.execute(select(NodeClaim))).scalars().first()
        node = await session.get(Node, claim.node_id)

    assert node.node_ref == NODE_REF
