"""Rate-limit counters kept in the database rather than in process memory.

A counter in memory is cleared by a restart and doubled by a second worker.
That is harmless for a limit protecting this server's own time, and not for one
bounding mail sent to somebody else's mailbox, which a restart would refill for
every caller at once. Two modules read and write the table, and nothing else
does: services/node_rate_limits.py, which says which of its limits count here,
and services/account_email.py, whose limits on changing an account's address
count here too, spent through node_rate_limits' own statement.
"""

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.users import Base


class RateCounter(Base):
    """How many requests one key has had admitted in its current window.

    One row per key per window length, so a key held to an hourly and a daily
    limit has two. A row whose `expires_at` has passed is dead rather than
    deleted at once: the next spend against its key starts a fresh window in
    place, and the next admission against any key deletes what is left, which
    is why `expires_at` is indexed.

    `key` is a SHA-256 of the limit's scope and the identity it counts, never
    the identity itself, so an address being limited is not stored in clear.
    Unsalted, it keeps the address out of a read of the table rather than out
    of reach of somebody who can guess it.
    """

    __tablename__ = "rate_counters"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_s: Mapped[int] = mapped_column(Integer, primary_key=True)
    window_start: Mapped[float] = mapped_column(Float)
    count: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
