"""Changing the address an account signs in with.

The design note's account flow (docs/design-notes/2026-09-17-email-node-claiming-design.md,
"Account address change"): the account, or an administrator for an account
whose owner lost the address, nominates a new address; the server mails it a
link; opening the link swaps the address in, and the one it replaces is told.
It changes no node's owner or address. A node belongs to the account rather
than to the string, and the address it was claimed with stays on it as the
record of that claim. What the change does end is every link still in the old
mailbox, sign-in and claim alike, so a claim that was waiting on one has to be
asked for again, and that mail goes to the new address.

The outgoing address is retired rather than freed (core/users.py,
RetiredAddress): it stays the account's, so a node reset and claimed with it
from habit reaches the same account, and no other account can take it. The
account can take it back, by the same change.

The link is a magic link under its own intent, so it can neither sign in nor
claim, and no other link can confirm a change. An account has one change
waiting at most (pending_address_changes): a new request replaces it and kills
the earlier link in the same transaction, as a node's claim challenge is
replaced. Confirming opens no session; it proves the new address and nothing
else.

An account asking for itself is never told whose an address is: another
account's address gets the answer a free one does, and no link. The other
account is told instead that the address cannot be added, at the address it
has now, which is where mail for one it retired goes too. A free address
holding as many links as it may gets that answer too, and nothing is sent,
since a taken one never fills up.

A deactivated account's address does not change: a request for one is
refused before anything is spent, and its link would not confirm either.

Every mail goes through the outbox in the transaction that makes it necessary
(services/mail_outbox.py): the confirmation to the new address, the notice to
the old one, which does not say where the account went, and the notice to an
address another account already has, which does not say who asked.
"""

import logging
import math
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import (
    INTENT_SIGNIN,
    MAGIC_LINK_EXPIRY_S,
    _hash_token,
    create_magic_link_in,
    peek_magic_link,
    redeem_magic_link_in,
)
from core.rate_counters import RateCounter
from core.users import (
    MagicLink,
    PendingAddressChange,
    RetiredAddress,
    User,
    _account_at,
    async_session_maker,
    forwarding_address,
    normalise_email,
)
from services import claim_links, mail, mail_outbox, mail_templates
from services.email_address import MAX_ADDRESS_LENGTH, is_address
from services.node_claim_store import drop_challenge, read_challenge
from services.node_rate_limits import CLAIM_LIMITS, _counter_key, _spend

logger = logging.getLogger(__name__)

# What the link is for, as `magic_links.intent` stores it.
INTENT_EMAIL_CHANGE = "email_change"

CHANGE_EXPIRY_S = MAGIC_LINK_EXPIRY_S

# The console's own route (AddressChangePage), at the root of HOST_APP, beside
# routes/auth.py's `_SIGN_IN_PATH` and services/claim_links.py's `_CLAIM_PATH`.
_CHANGE_PATH = "/auth/email/"

# How long either notice is worth sending. It carries no link to die, but a
# notice held up for longer than this by an outage says something its reader
# has long since found out, or no longer cares to.
_NOTICE_EXPIRY_S = 86400.0

# The answers the console shows verbatim.
DEAD_LINK = "That link is no longer valid"
TAKEN = "That address belongs to another account"
_NOT_AN_ADDRESS = "That is not an email address"
_ALREADY_THIS = "That is already this account's address"
_RATE_LIMITED = "Too many address changes have been asked for; try again later"
_UNAVAILABLE = "Changing the address by email is unavailable"
_NO_ACCOUNT = "User not found"
_DEACTIVATED = "Account is deactivated"
_NOT_SIGNED_IN = "Not authenticated"

# Sign-in's own grammar (routes/auth.py, MagicLinkRequest), since the new
# address is what the account signs in with from now on.
_SIGN_IN_ADDRESS = TypeAdapter(EmailStr)


class Refused(Exception):
    """A request or a confirmation that was refused, and the answer it gets.

    Carried as data rather than as a response: the routes own FastAPI, and the
    account's own route and the administrator's give the same answers.
    """

    def __init__(self, status_code: int, detail: str, *, retry_after_s: int | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class Requested:
    """A change that is waiting for its link: the account, the new address, and
    how long the link lives."""

    user_id: str
    email: str
    expires_in: int


def _deactivated(*, by_account: bool) -> Refused:
    """The refusal for an account that is deactivated.

    409 for an administrator, who is told why. Asked `by_account`, it is the
    account's own cookie that resolved before the deactivation ended every
    session it had (core/sessions.py, deactivate_account): 401, as any route
    behind the session answers a cookie that no longer signs in, so the
    console ends the session.
    """
    return Refused(401, _NOT_SIGNED_IN) if by_account else Refused(409, _DEACTIVATED)


def _account_id(user_id: str | uuid.UUID) -> uuid.UUID | None:
    if isinstance(user_id, uuid.UUID):
        return user_id
    try:
        return uuid.UUID(user_id)
    except ValueError:
        return None


def _address(raw: str) -> str:
    """`raw` normalised, if it is an address both sign-in and a node's claim
    accept. Raises Refused(400) otherwise.

    Both, because the new address is the account's key from now on: sign-in
    has to take it, and so does a node offered it, or the account could not
    claim a node with its own address.
    """
    address = normalise_email(raw)
    if len(address) > MAX_ADDRESS_LENGTH or not is_address(address):
        raise Refused(400, _NOT_AN_ADDRESS)
    try:
        _SIGN_IN_ADDRESS.validate_python(address)
    except ValidationError:
        raise Refused(400, _NOT_AN_ADDRESS) from None
    return address


async def _holder(session: AsyncSession, address: str, account_id: uuid.UUID) -> User | None:
    """The other account `address` belongs to, as its address or as one it
    retired, or None when it is nobody's but possibly this account's."""
    holder = await _account_at(session, address)
    return holder if holder is not None and holder.id != account_id else None


async def _admit(session: AsyncSession, user_key: str, address: str) -> int | None:
    """Spend one request for this account and this address, or answer the wait
    in seconds.

    services/node_rate_limits.py's ClaimRateLimiter, with its numbers, over
    counters of their own: both limits are spent together or neither is, in
    one transaction on `session`, committed only when every counter had room.
    The address half bounds how much mail one mailbox can be sent from our
    domain, notices included. The account half bounds how often an account,
    or an administrator acting for one, can ask, and so how fast the
    administrator's 409 can be asked whose an address is.
    """
    now = time.time()
    try:
        # The account's counters first, so when both are full the wait counts
        # down the caller's own window.
        for key in (_counter_key("email_change", user_key), _counter_key("email_change_address", address)):
            for limit, window_s in CLAIM_LIMITS:
                count, window_start = (await session.execute(_spend(key, window_s, now))).one()
                if count > limit:
                    await session.rollback()
                    return max(1, math.ceil(window_start + window_s - now))
        await session.execute(delete(RateCounter).where(RateCounter.expires_at < now))
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return None


async def request_change(user_id: str | uuid.UUID, email: str, *, by_account: bool = False) -> Requested:
    """Mail a link that makes `email` the account's address, replacing any
    change it had waiting. Raises Refused for every answer but that.

    404 for an account that does not exist; 409 for one that is deactivated,
    or 401 asked `by_account` (_deactivated); 503 when no mail can leave,
    asked before anything is written; 400 for something that is not an
    address, or that is the account's address already; 429 past the limits.
    One of the account's own retired addresses is free to it.

    Another account's address, current or retired, is 409 for an
    administrator acting for the account. Asked `by_account`, by the account
    for itself, it is answered as a free address is, so that no signed-in
    account can ask whose an address is, as sign-in cannot be asked either.
    No link is minted, and the change the account had waiting goes as a new
    request's link would replace it. The other account is sent a notice that
    the address cannot be added (_turn_away), at the address it has now, the
    one asked for or the one a retired address forwards to, so one mail is
    queued whichever it was, as one is for a free address.

    A free address can hold as many live links of this kind as the link
    store allows (core/auth.py), which several accounts asking for it at once
    reach inside the limits. That is 429 for an administrator. Asked
    `by_account`, it is answered as a taken address is, which never reaches
    that cap, so the cap cannot tell the two apart: nothing new is minted, the
    links already out for the address still work, and the change the account
    had waiting goes.

    The limits are spent before whose address it is gets answered, so the
    answer cannot be asked for faster than they allow, and a notice is held to
    the address's limit as a link is.

    Everything after the limits is one transaction, which takes SQLite's write
    lock before it reads whose the address is (_hold), so what it finds holds
    until the link, the waiting change and the mail commit together.
    """
    account_id = _account_id(user_id)
    if account_id is None:
        raise Refused(404, _NO_ACCOUNT)
    user_key = str(account_id)
    async with async_session_maker() as session:
        account = await session.get(User, account_id)
        if account is None:
            raise Refused(404, _NO_ACCOUNT)
        if not account.is_active:
            raise _deactivated(by_account=by_account)
        # About this deployment, not about the address. The link is built on
        # the one reading of HOST_APP taken here.
        reason = mail_outbox.unavailable()
        landing = mail.link_to(_CHANGE_PATH)
        if reason is not None or landing is None:
            logger.error("Changing an address by email is unavailable: %s", reason or "HOST_APP is not set")
            raise Refused(503, _UNAVAILABLE)
        address = _address(email)
        if normalise_email(account.email) == address:
            raise Refused(400, _ALREADY_THIS)
        wait_s = await _admit(session, user_key, address)
        if wait_s is not None:
            raise Refused(429, _RATE_LIMITED, retry_after_s=wait_s)

    now = time.time()
    try:
        async with async_session_maker() as session:
            async with session.begin():
                holder = await _hold(session, account_id, address, by_account=by_account)
                if holder is None:
                    await _send_link(session, user_key, address, landing, now, by_account=by_account)
                elif by_account:
                    await _turn_away(session, user_key, holder, address, now)
                else:
                    raise Refused(409, TAKEN)
    except mail_outbox.OutboxKeyError:
        # The key went bad since unavailable() was asked. Rolled back whole, so
        # a change already waiting keeps its link.
        logger.error("A mail about a new address could not be queued, so nothing was sent")
        raise Refused(503, _UNAVAILABLE) from None
    mail_outbox.wake()
    return Requested(user_id=user_key, email=address, expires_in=CHANGE_EXPIRY_S)


async def _hold(session: AsyncSession, account_id: uuid.UUID, address: str, *, by_account: bool) -> User | None:
    """Take SQLite's write lock on the account's row, check again what
    request_change checked before it spent the limits, and return the other
    account `address` belongs to, or None when it is free to this one.

    pysqlite begins the transaction at its first write, so this UPDATE, which
    changes nothing, is what makes the reads after it hold until the commit.
    """
    row = (
        await session.execute(
            update(User)
            .where(User.id == account_id)
            .values(email=User.email)
            .returning(User.email, User.is_active)
            .execution_options(synchronize_session=False)
        )
    ).one_or_none()
    if row is None:
        raise Refused(404, _NO_ACCOUNT)
    current, active = row
    # Deactivated since it was read, which a confirmation would refuse anyway.
    if not active:
        raise _deactivated(by_account=by_account)
    # A change to this address the account confirmed since it asked.
    if normalise_email(current) == address:
        raise Refused(400, _ALREADY_THIS)
    return await _holder(session, address, account_id)


async def _send_link(
    session: AsyncSession, user_key: str, address: str, landing: str, now: float, *, by_account: bool
) -> None:
    """Mint the link that confirms `address`, make it the account's waiting
    change, and queue its mail, in request_change's transaction."""
    token = await create_magic_link_in(session, address, intent=INTENT_EMAIL_CHANGE, now=now)
    if token is None:
        # The address holds as many live links of this kind as it may, which
        # several accounts asking for it at once can reach inside the limits.
        if not by_account:
            raise Refused(429, _RATE_LIMITED, retry_after_s=CHANGE_EXPIRY_S)
        # A taken address never fills up, as no link is minted for one, so a
        # wait here would say this one is free. Answered as a taken one is:
        # nothing new is minted, the links already out for it still work, and
        # the change the account had waiting goes.
        await _drop_waiting(session, user_key)
        logger.info("Account %s asked for an address holding all the links it may and was sent no link", user_key)
        return
    displaced = await _put_pending(session, user_key, address, _hash_token(token), now)
    if displaced is not None:
        # Dies with the row that named it, so a link mailed to a mistyped
        # address stops working once it is corrected.
        await session.execute(delete(MagicLink).where(MagicLink.token_hash == displaced))
    rendered = mail_templates.address_change(landing + token, CHANGE_EXPIRY_S // 60)
    await mail_outbox.enqueue(
        session,
        mail.Message(to=address, subject=rendered.subject, text=rendered.text, html=rendered.html),
        kind=mail_outbox.KIND_ADDRESS_CHANGE,
        # The link's own expiry, so a mail held up past it is never sent.
        expires_at=now + CHANGE_EXPIRY_S,
        now=now,
    )
    logger.info("Account %s has an address change waiting for its link", user_key)


async def _turn_away(session: AsyncSession, user_key: str, holder: User, address: str, now: float) -> None:
    """What an account asking for another account's address gets in place of a
    link, in request_change's transaction.

    Its waiting change goes, as a link to a free address would replace it, so
    a link it holds from before dies whichever address it asked for next.
    `holder` is told, at the address as it holds it: the one asked for when
    that is its own, and when it is one `holder` retired, the one it has now,
    naming the retired one, as all mail for a retired address goes
    (core/users.py, forwarding_address). So one notice is queued either way,
    as one link is for a free address, and the work a request does cannot
    tell a retired address from one in use or a free one.
    """
    await _drop_waiting(session, user_key)
    former = None if normalise_email(holder.email) == address else address
    rendered = mail_templates.address_taken(former=former)
    await mail_outbox.enqueue(
        session,
        mail.Message(to=holder.email, subject=rendered.subject, text=rendered.text, html=rendered.html),
        kind=mail_outbox.KIND_ADDRESS_TAKEN,
        expires_at=now + _NOTICE_EXPIRY_S,
        now=now,
    )
    logger.info("Account %s asked for another account's address and was sent no link", user_key)


async def _drop_waiting(session: AsyncSession, user_key: str) -> None:
    """End the change the account has waiting, and kill its link, in
    request_change's transaction."""
    waiting = await session.get(PendingAddressChange, user_key)
    if waiting is not None:
        await session.execute(delete(MagicLink).where(MagicLink.token_hash == waiting.handle))
        await session.delete(waiting)


async def _put_pending(session: AsyncSession, user_key: str, address: str, handle: str, now: float) -> str | None:
    """Make this the account's waiting change, returning the handle it displaced."""
    row = await session.get(PendingAddressChange, user_key)
    if row is None:
        session.add(
            PendingAddressChange(
                user_id=user_key, email=address, handle=handle, created_at=now, expires_at=now + CHANGE_EXPIRY_S
            )
        )
        await session.flush()
        return None
    displaced = row.handle
    row.email = address
    row.handle = handle
    row.created_at = now
    row.expires_at = now + CHANGE_EXPIRY_S
    await session.flush()
    return displaced


async def _waiting(session: AsyncSession, token: str) -> PendingAddressChange | None:
    """The waiting change whose link `token` is, or None."""
    return (
        await session.execute(select(PendingAddressChange).where(PendingAddressChange.handle == _hash_token(token)))
    ).scalar_one_or_none()


async def preview(token: str) -> str | None:
    """The address a link would make the account's, without spending it, or
    None for a link that would not.

    For the landing page, which names the address and waits for a press, so a
    scanner that renders it leaves the link alive. Reading it takes the same
    secret confirming does.
    """
    link = await peek_magic_link(token, intent=INTENT_EMAIL_CHANGE)
    if link is None:
        return None
    async with async_session_maker() as session:
        waiting = await _waiting(session, token)
    return link.email if waiting is not None and waiting.email == link.email else None


async def confirm(token: str) -> str:
    """Open a change's link: the new address becomes the account's, and the old
    one is retired to it. Returns the new address. Raises Refused otherwise.

    400 for a link that is unknown, expired, spent, displaced by a later
    request, or for an account that is gone or deactivated: one answer for
    all, as sign-in gives. 409 when the address became somebody else's since
    it was asked for. 503 when the notice to the old address cannot be queued,
    since the change and its notice are one.

    The sign-in and claim links still in the old mailbox die in the same
    transaction, a claim's with its challenge and its queued mail.

    One transaction, whose first write spends the link, so from there on
    nothing it reads can change before the commit, and a refusal rolls the
    spend back with everything else: the link still works if the refusal
    stops applying, a deactivated account reactivated or the address freed,
    before it expires.
    """
    now = time.time()
    try:
        async with async_session_maker() as session:
            async with session.begin():
                user_key, address = await _swap(session, token, now)
    except mail_outbox.OutboxKeyError:
        logger.error("The notice of a changed address could not be queued, so the address was not changed")
        raise Refused(503, _UNAVAILABLE) from None
    mail_outbox.wake()
    logger.info("Account %s changed the address it signs in with", user_key)
    return address


async def _swap(session: AsyncSession, token: str, now: float) -> tuple[str, str]:
    """confirm's transaction, up to its commit. Returns the account and its new address."""
    link = await redeem_magic_link_in(session, token, intent=INTENT_EMAIL_CHANGE, now=now)
    if link is None:
        raise Refused(400, DEAD_LINK)
    # Checked against the waiting change rather than only for being live, so
    # a link a later request displaced is dead even if its row survived.
    waiting = await _waiting(session, token)
    if waiting is None or waiting.email != link.email:
        raise Refused(400, DEAD_LINK)
    user_key, address = waiting.user_id, waiting.email
    account = await session.get(User, uuid.UUID(user_key))
    # Deactivated, no link of any kind is redeemed for it (core/users.py, User).
    if account is None or not account.is_active:
        raise Refused(400, DEAD_LINK)
    await session.delete(waiting)
    old = account.email
    if normalise_email(old) != address:
        if await _holder(session, address, account.id) is not None:
            raise Refused(409, TAKEN)
        # Read before the retired addresses change below: mail for those the
        # account retired before has been going to the old address too.
        mailed_to_old = [normalise_email(old), *await _retired_by(session, user_key)]
        # An address the account retired before is its own again.
        await session.execute(
            delete(RetiredAddress).where(RetiredAddress.email == address, RetiredAddress.user_id == user_key)
        )
        # The one it leaves is its own to retire, though another account may
        # have retired it before this one had it, which a rollback across
        # migration 0025 leaves. The account that has an address wins it
        # (core/users.py, _account_at), so that row goes, rather than hand the
        # address back to the account that left it.
        await session.execute(delete(RetiredAddress).where(RetiredAddress.email == normalise_email(old)))
        session.add(RetiredAddress(email=normalise_email(old), user_id=user_key, retired_at=datetime.now(UTC)))
        account.email = address
        # A sign-in link still in the old mailbox would otherwise sign whoever
        # reads it in, to an account that has just moved away from it.
        await session.execute(
            delete(MagicLink).where(
                MagicLink.email == normalise_email(old),
                MagicLink.intent == INTENT_SIGNIN,
                MagicLink.used_at.is_(None),
            )
        )
        # So would a claim link there, binding its node to the account first.
        await _withdraw_claims(session, mailed_to_old)
        rendered = mail_templates.address_changed()
        await mail_outbox.enqueue(
            session,
            mail.Message(to=old, subject=rendered.subject, text=rendered.text, html=rendered.html),
            kind=mail_outbox.KIND_ADDRESS_NOTICE,
            expires_at=now + _NOTICE_EXPIRY_S,
            now=now,
        )
    # Opening a link sent there proves the address, as a sign-in does.
    account.is_verified = True
    return user_key, address


async def _retired_by(session: AsyncSession, user_key: str) -> list[str]:
    """The addresses the account has retired whose mail comes to it: each of
    them but one another account has as its own address since, which is that
    account's and is mailed itself (core/users.py, forwarding_address)."""
    retired = (await session.execute(select(RetiredAddress.email).where(RetiredAddress.user_id == user_key))).scalars()
    return [email for email in retired.all() if await forwarding_address(session, email) is not None]


async def _withdraw_claims(session: AsyncSession, addresses: list[str]) -> None:
    """Kill every claim link still out for `addresses`, in confirm's transaction,
    and with it the challenge it was for and the claim mail queued to carry it.

    Those are the links in the old mailbox: the ones for the old address, and
    the ones for addresses the account retired before, whose mail
    claim_links.queue forwarded there. Any of them finds the account through
    the address it names, so whoever reads that mailbox could bind its node to
    the account and be signed in by the click.

    A node waiting on one reads unclaimed with its address on file, as a
    declined link leaves it, so whoever is at the node can ask again, and that
    mail goes to the account's new address. A node whose challenge is another
    link by now keeps it, and the mail queued for it.
    """
    links = (
        await session.execute(
            select(MagicLink.token_hash, MagicLink.node_id).where(
                MagicLink.email.in_(addresses),
                MagicLink.intent == claim_links.INTENT_CLAIM,
                MagicLink.used_at.is_(None),
            )
        )
    ).all()
    for handle, node_id in links:
        await claim_links.invalidate_in(session, handle)
        if node_id is None:
            continue
        challenge = await read_challenge(session, node_id)
        if challenge is not None and challenge.handle == handle:
            # Every claim mail queued for the node carries this challenge's
            # link, since a new challenge cancels the mail queued for the one
            # it replaces.
            await mail_outbox.cancel(session, node_id=node_id)
            await drop_challenge(session, node_id)
