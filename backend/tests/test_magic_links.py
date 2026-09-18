"""The magic-link store in core/auth.py.

These cover the token's lifecycle rather than the routes that drive it: issue,
redeem once, and the several ways a redemption must fail. tests/test_auth_routes.py
covers what the endpoints do with the answers.
"""

import hashlib
import time

import pytest
from sqlalchemy import select

from core.auth import (
    INTENT_SIGNIN,
    MAGIC_LINK_EXPIRY_S,
    _hash_token,
    consume_magic_link,
    create_magic_link,
    invalidate_magic_link,
    peek_magic_link,
    redeem_magic_link,
)
from core.users import MagicLink


async def _rows(session_maker):
    async with session_maker() as session:
        result = await session.execute(select(MagicLink))
        return result.scalars().all()


@pytest.mark.asyncio
class TestCreateMagicLink:
    async def test_returns_a_token_and_stores_only_its_hash(self):
        from core.users import async_session_maker

        token = await create_magic_link("owner@example.com")
        assert token

        rows = await _rows(async_session_maker)
        assert len(rows) == 1
        assert rows[0].token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert rows[0].token_hash != token

    async def test_the_raw_token_appears_nowhere_in_the_row(self):
        from core.users import async_session_maker

        token = await create_magic_link("owner@example.com")
        (row,) = await _rows(async_session_maker)
        stored = f"{row.token_hash}{row.email}"
        assert token not in stored

    async def test_email_is_normalised(self):
        from core.users import async_session_maker

        await create_magic_link("  Owner@Example.COM  ")
        (row,) = await _rows(async_session_maker)
        assert row.email == "owner@example.com"

    async def test_expiry_is_fifteen_minutes(self):
        from core.users import async_session_maker

        before = time.time()
        await create_magic_link("owner@example.com")
        (row,) = await _rows(async_session_maker)
        assert MAGIC_LINK_EXPIRY_S == 900
        assert before + MAGIC_LINK_EXPIRY_S <= row.expires_at <= time.time() + MAGIC_LINK_EXPIRY_S

    async def test_two_requests_give_two_different_tokens(self):
        first = await create_magic_link("owner@example.com")
        second = await create_magic_link("owner@example.com")
        assert first != second

    async def test_asking_again_does_not_invalidate_the_outstanding_link(self):
        """A second request while the first mail is still in flight must not
        strand whichever one the person opens."""
        first = await create_magic_link("owner@example.com")
        await create_magic_link("owner@example.com")
        assert await consume_magic_link(first) == "owner@example.com"


@pytest.mark.asyncio
class TestConsumeMagicLink:
    async def test_returns_the_email(self):
        token = await create_magic_link("owner@example.com")
        assert await consume_magic_link(token) == "owner@example.com"

    async def test_a_second_redemption_fails(self):
        token = await create_magic_link("owner@example.com")
        await consume_magic_link(token)
        assert await consume_magic_link(token) is None

    async def test_an_expired_link_fails(self, monkeypatch):
        from core.users import async_session_maker

        token = await create_magic_link("owner@example.com")
        async with async_session_maker() as session:
            (row,) = (await session.execute(select(MagicLink))).scalars().all()
            row.expires_at = time.time() - 1
            await session.commit()
        assert await consume_magic_link(token) is None

    async def test_an_unknown_token_fails(self):
        assert await consume_magic_link("not-a-token-anyone-issued") is None

    @pytest.mark.parametrize("token", ["", "   ", None])
    async def test_an_empty_token_fails_without_touching_the_database(self, token):
        assert await consume_magic_link(token) is None

    async def test_redeeming_invalidates_the_addresss_other_links(self):
        """A link the person abandoned must not stay usable by whoever else can
        read that mailbox later."""
        abandoned = await create_magic_link("owner@example.com")
        used = await create_magic_link("owner@example.com")
        assert await consume_magic_link(used) == "owner@example.com"
        assert await consume_magic_link(abandoned) is None

    async def test_redeeming_leaves_another_addresss_links_alone(self):
        theirs = await create_magic_link("someone-else@example.com")
        mine = await create_magic_link("owner@example.com")
        await consume_magic_link(mine)
        assert await consume_magic_link(theirs) == "someone-else@example.com"

    async def test_a_used_link_is_not_deleted(self):
        """The row stays so a replay is distinguishable from a token that never
        existed, in the logs if not in the response."""
        from core.users import async_session_maker

        token = await create_magic_link("owner@example.com")
        await consume_magic_link(token)
        (row,) = await _rows(async_session_maker)
        assert row.used_at is not None


@pytest.mark.asyncio
class TestOutstandingLinkCap:
    async def test_beyond_the_cap_no_link_is_issued(self):
        from core.auth import _MAX_OUTSTANDING_MAGIC_LINKS

        for _ in range(_MAX_OUTSTANDING_MAGIC_LINKS):
            assert await create_magic_link("owner@example.com")
        assert await create_magic_link("owner@example.com") is None

    async def test_the_cap_is_per_address(self):
        from core.auth import _MAX_OUTSTANDING_MAGIC_LINKS

        for _ in range(_MAX_OUTSTANDING_MAGIC_LINKS):
            await create_magic_link("owner@example.com")
        assert await create_magic_link("someone-else@example.com")

    async def test_expired_links_do_not_count_against_the_cap(self):
        from core.auth import _MAX_OUTSTANDING_MAGIC_LINKS
        from core.users import async_session_maker

        for _ in range(_MAX_OUTSTANDING_MAGIC_LINKS):
            await create_magic_link("owner@example.com")
        async with async_session_maker() as session:
            for row in (await session.execute(select(MagicLink))).scalars().all():
                row.expires_at = time.time() - 1
            await session.commit()
        assert await create_magic_link("owner@example.com")

    async def test_expired_rows_are_cleared_rather_than_accumulating(self):
        from core.users import async_session_maker

        await create_magic_link("owner@example.com")
        async with async_session_maker() as session:
            for row in (await session.execute(select(MagicLink))).scalars().all():
                row.expires_at = time.time() - 1
            await session.commit()
        await create_magic_link("owner@example.com")
        assert len(await _rows(async_session_maker)) == 1


# ── Intent ───────────────────────────────────────────────────────────────────
#
# A link is a bearer credential in a mailbox, and mailboxes get forwarded. What
# a token may do therefore travels with it rather than being inferred from which
# endpoint happened to receive it.

CLAIM = "claim"
ADDRESS = "ada@example.com"


async def test_a_claim_link_cannot_be_redeemed_as_a_sign_in():
    """The forwarded-link attack: whoever is sent a claim link for somebody
    else's node must not be able to spend it as a session on their account."""
    token = await create_magic_link(ADDRESS, intent=CLAIM, node_id="ret1a2b3c4d")

    assert await consume_magic_link(token) is None


async def test_a_sign_in_link_cannot_be_redeemed_as_a_claim():
    token = await create_magic_link(ADDRESS)

    assert await redeem_magic_link(token, intent=CLAIM) is None


async def test_a_refusal_by_intent_leaves_the_link_usable_by_its_rightful_redeemer():
    """The intent is part of the WHERE rather than a check afterwards, so the
    endpoint that refused a link has not quietly spent it."""
    token = await create_magic_link(ADDRESS, intent=CLAIM, node_id="ret1a2b3c4d")
    assert await consume_magic_link(token) is None

    redeemed = await redeem_magic_link(token, intent=CLAIM)

    assert redeemed is not None
    assert redeemed.node_id == "ret1a2b3c4d"


async def test_signing_in_does_not_wipe_an_outstanding_claim_link():
    """The sweep is what stops an abandoned link in a readable mailbox opening a
    session. Unscoped it would also wipe the challenge for a node its owner is
    halfway through setting up, leaving them at `pending` with nothing to click."""
    claim_token = await create_magic_link(ADDRESS, intent=CLAIM, node_id="ret1a2b3c4d")
    signin_token = await create_magic_link(ADDRESS)

    assert await consume_magic_link(signin_token) == ADDRESS

    assert await redeem_magic_link(claim_token, intent=CLAIM) is not None


async def test_claiming_does_not_wipe_an_outstanding_sign_in_link():
    signin_token = await create_magic_link(ADDRESS)
    claim_token = await create_magic_link(ADDRESS, intent=CLAIM, node_id="ret1a2b3c4d")

    assert await redeem_magic_link(claim_token, intent=CLAIM) is not None

    assert await consume_magic_link(signin_token) == ADDRESS


async def test_the_sweep_still_clears_abandoned_links_of_the_same_intent():
    first = await create_magic_link(ADDRESS)
    second = await create_magic_link(ADDRESS)

    assert await consume_magic_link(second) == ADDRESS

    assert await consume_magic_link(first) is None


async def test_claiming_one_node_does_not_wipe_the_link_for_another():
    """One address bringing up two nodes holds a claim link for each. Spending
    one must leave the other, or that node waits out its expiry with nothing to
    click."""
    first = await create_magic_link(ADDRESS, intent=CLAIM, node_id="ret1a2b3c4d")
    second = await create_magic_link(ADDRESS, intent=CLAIM, node_id="retdeadbeef")

    assert await redeem_magic_link(first, intent=CLAIM) is not None

    redeemed = await redeem_magic_link(second, intent=CLAIM)
    assert redeemed is not None
    assert redeemed.node_id == "retdeadbeef"


async def test_the_cap_is_counted_per_intent():
    """Somebody bringing up four nodes and signing in twice would otherwise
    exhaust one shared allowance, and create_magic_link answers None silently."""
    for _ in range(5):
        assert await create_magic_link(ADDRESS) is not None
    assert await create_magic_link(ADDRESS) is None

    assert await create_magic_link(ADDRESS, intent=CLAIM, node_id="ret1a2b3c4d") is not None


async def test_the_node_travels_with_a_claim_link():
    token = await create_magic_link(ADDRESS, intent=CLAIM, node_id="ret1a2b3c4d")

    redeemed = await redeem_magic_link(token, intent=CLAIM)

    assert (redeemed.email, redeemed.intent, redeemed.node_id) == (ADDRESS, CLAIM, "ret1a2b3c4d")


async def test_a_sign_in_link_is_about_nobody_s_node():
    token = await create_magic_link(ADDRESS)

    redeemed = await redeem_magic_link(token, intent=INTENT_SIGNIN)

    assert redeemed.node_id is None


# ── Peeking and invalidating ─────────────────────────────────────────────────


async def test_peeking_does_not_spend_the_link():
    token = await create_magic_link(ADDRESS, intent=CLAIM, node_id="ret1a2b3c4d")

    assert (await peek_magic_link(token, intent=CLAIM)).node_id == "ret1a2b3c4d"
    assert (await peek_magic_link(token, intent=CLAIM)).node_id == "ret1a2b3c4d"

    assert await redeem_magic_link(token, intent=CLAIM) is not None


async def test_peeking_is_bound_by_intent_like_redeeming():
    token = await create_magic_link(ADDRESS, intent=CLAIM, node_id="ret1a2b3c4d")

    assert await peek_magic_link(token, intent=INTENT_SIGNIN) is None


async def test_peeking_a_spent_link_gives_nothing():
    token = await create_magic_link(ADDRESS, intent=CLAIM, node_id="ret1a2b3c4d")
    await redeem_magic_link(token, intent=CLAIM)

    assert await peek_magic_link(token, intent=CLAIM) is None


async def test_invalidating_makes_a_link_unredeemable():
    token = await create_magic_link(ADDRESS, intent=CLAIM, node_id="ret1a2b3c4d")

    await invalidate_magic_link(_hash_token(token))

    assert await redeem_magic_link(token, intent=CLAIM) is None


async def test_invalidating_an_unknown_handle_is_not_an_error():
    await invalidate_magic_link("0" * 64)


async def test_invalidating_one_link_leaves_the_others_alone():
    doomed = await create_magic_link(ADDRESS, intent=CLAIM, node_id="ret1a2b3c4d")
    kept = await create_magic_link(ADDRESS, intent=CLAIM, node_id="retdeadbeef")

    await invalidate_magic_link(_hash_token(doomed))

    assert await redeem_magic_link(kept, intent=CLAIM) is not None
