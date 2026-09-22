"""Node ids: a three-letter system prefix and eight lowercase hex digits.

The prefix names the system a node belongs to, so nothing records the system
separately: code that has to branch on it reads the prefix with `system_of`. A
new device family adds a prefix here rather than a column or a second regex.
"""

import re
import secrets
from collections.abc import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# The Pi 5 fleet. The hex is derived from the board serial and claimed by the
# node at registration, so the server never mints one.
FLEET = "ret"
# Stock 30hours/blah2 radars the server polls. The box exposes nothing intrinsic
# to derive an id from, and the address is not an identity, so the server mints.
POLLED_BLAH2 = "bla"

SYSTEMS = (FLEET, POLLED_BLAH2)
MINTED_SYSTEMS = (POLLED_BLAH2,)

MINT_ATTEMPTS = 5

_SHAPE = re.compile(r"^([a-z]{3})[0-9a-f]{8}$")


def node_id_pattern(system: str) -> str:
    return rf"^{system}[0-9a-f]{{8}}$"


def system_of(node_id: str) -> str | None:
    """The system prefix of a well-formed node id, or None for anything else."""
    match = _SHAPE.match(node_id)
    if match is None or match.group(1) not in SYSTEMS:
        return None
    return match.group(1)


async def add_with_minted_id[Row](session: AsyncSession, system: str, build: Callable[[str], Row]) -> Row:
    """Flush the row `build(node_id)` returns under a freshly minted node id.

    Uniqueness is the database's, not the generator's: each attempt runs in a
    savepoint, so a clash rolls back that attempt alone and leaves the caller's
    transaction intact. `build` must return the row whose key is the node id and
    nothing that can clash on another unique column, or every attempt fails alike
    and the last IntegrityError is raised. The caller commits.
    """
    if system not in MINTED_SYSTEMS:
        raise ValueError(f"{system!r} ids are not minted by the server")
    # begin_nested flushes pending work first; do it here so a fault in the
    # caller's own rows raises as itself rather than as a clash to retry.
    await session.flush()
    for attempt in range(1, MINT_ATTEMPTS + 1):
        row = build(system + secrets.token_hex(4))
        try:
            async with session.begin_nested():
                session.add(row)
        except IntegrityError:
            if attempt == MINT_ATTEMPTS:
                raise
        else:
            return row
