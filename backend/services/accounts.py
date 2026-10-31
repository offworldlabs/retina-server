"""An account's own settings, an owner handing a node back, and deleting an account.

There is one release path. The owner's release route (routes/owner.py) and
deleting an account both hand a node back through release_owned_node, so a
node leaves an account the same way whoever asked.
"""

import logging
import unicodedata
import uuid

from sqlalchemy import update

from core import sessions
from core.auth import get_user_nodes
from core.node_ids import POLLED_BLAH2, system_of
from core.users import User, async_session_maker, parse_account_id
from services import polled_registration
from services.node_claiming import release_node
from services.polled_radars import remove_polled_radar

logger = logging.getLogger(__name__)

#: The longest display name, in characters, once trimmed: as long as a node's label.
NAME_MAX_CHARS = 64

# Controls and format characters, the second of which reorder or hide the text
# around them, and what breaks a one-line name as a control would. A lone
# surrogate is here too: it decodes from JSON and cannot be stored.
_UNPRINTABLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

# How many times a deletion hands back what the account owns and tries to
# remove it, when a claim binds another node to it in between. Each of those
# needs a link clicked in that moment, so even a second round is rare.
_DELETION_ROUNDS = 3

#: Who asked, for delete_account, when it is the account itself. The event log
#: is kept on disk (routes/admin.py), and naming the account by its own address
#: there would keep the address the deletion was meant to remove. The event
#: names the account by its id anyway.
SELF = "self"


class NameRefused(ValueError):
    """A display name an account may not take, carrying the reason to show them."""


def _display_name(name: str) -> str:
    """`name` trimmed, or NameRefused for one that is empty, too long, or
    carries a control or format character."""
    trimmed = name.strip()
    if not trimmed:
        raise NameRefused("A name cannot be empty.")
    if len(trimmed) > NAME_MAX_CHARS:
        raise NameRefused(f"A name is at most {NAME_MAX_CHARS} characters.")
    if any(unicodedata.category(c) in _UNPRINTABLE for c in trimmed):
        raise NameRefused("A name cannot contain control characters.")
    return trimmed


async def rename(user_id: uuid.UUID, name: str) -> User | None:
    """Give an active account the display name `name`, trimmed, and return the account as it now is.

    Raises NameRefused for a name it may not take, before anything is written.
    None when the account is gone or deactivated, which the write checks
    rather than the caller's copy of the account, as record_visit does.
    """
    name = _display_name(name)
    async with async_session_maker() as session:
        async with session.begin():
            return await session.scalar(
                update(User).where(User.id == user_id, User.is_active.is_(True)).values(name=name).returning(User)
            )


async def release_owned_node(node_id: str, user_id: str) -> bool:
    """Hand one of `user_id`'s nodes back, or remove it if it is a polled radar.

    False, changing nothing, when they do not own it. A polled radar is removed
    rather than handed back, since nothing can claim one afterwards
    (services/polled_radars.py, retire_polled_radar), and the poller lets go of
    it once that has committed.
    """
    if system_of(node_id) != POLLED_BLAH2:
        return await release_node(node_id, user_id)
    async with async_session_maker() as session:
        async with session.begin():
            if not await remove_polled_radar(session, node_id, user_id=user_id):
                return False
    polled_registration.let_go(node_id)
    return True


async def delete_account(user_id: str | uuid.UUID, *, by: str) -> int | None:
    """Delete an account: hand back every node it owns, end every session, and remove its row.

    Returns how many nodes this call handed back, or None, touching nothing,
    when there is no such account: a node can name an owner with no account
    row, an administrator's Access identity, and those are not this one's to
    hand back. `by` is who asked, as the event log names them: SELF for the
    account itself, or an administrator's identity.

    The nodes go first, each through the owner's own release, while the
    account can still sign in. The row goes last, in the one transaction that
    also ends its sessions and kills the sign-in links to its address, and
    only once no node names it (core/sessions.py, remove_account). So a
    failure part way leaves an account that still signs in and still owns
    what was not handed back yet, and running this again, by its owner or by
    an administrator, finishes the job.

    A claim that binds a node to the account between the release and the
    removal stops the removal, and the round runs again. Raises
    AccountHoldsNodes if that happens _DELETION_ROUNDS times running, with the
    account still there to delete. A claim that binds after the removal finds
    no account to bind to.
    """
    account_id = parse_account_id(user_id)
    if account_id is None:
        return None
    async with async_session_maker() as session:
        if await session.get(User, account_id) is None:
            return None
    user_key = str(account_id)
    released: list[str] = []
    for _ in range(_DELETION_ROUNDS):
        for node_id in await get_user_nodes(user_key):
            if await release_owned_node(node_id, user_key):
                released.append(node_id)
        try:
            removed = await sessions.remove_account(account_id)
        except sessions.AccountHoldsNodes:
            continue
        break
    else:
        raise sessions.AccountHoldsNodes
    # False when another deletion of the same account removed it first. That
    # one records it.
    if removed:
        _log_event("user", f"Account {user_key} deleted", "warning", {"user_id": user_key, "by": by, "nodes": released})
    return len(released)


# Lazy import for the reason services/tcp_handler.py gives: routes.admin must be
# importable first. The deletion has committed by then, so a failure to record
# it is logged rather than raised.
def _log_event(category: str, message: str, severity: str, meta: dict) -> None:
    try:
        from routes.admin import log_event

        log_event(category, message, severity, meta)
    except Exception:
        logger.warning("Could not record an account deletion in the event log", exc_info=True)
