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
    MAGIC_LINK_EXPIRY_S,
    consume_magic_link,
    create_magic_link,
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
