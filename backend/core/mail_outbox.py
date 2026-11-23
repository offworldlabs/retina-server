"""Mail waiting to be sent, and the record of what became of it.

A row is written in the same transaction as whatever made the mail necessary,
so a link that was minted is a link that will be sent, and one whose minting
rolled back is never sent. services/mail_outbox.py sends it and settles it.

The body is Fernet ciphertext and is wiped once the row settles. It carries a
sign-in or claim link, and those exist in the mail and nowhere else: users.db
is copied off the host every day, and a reader of that copy must not find a
credential in it. The key is kept beside the database file rather than in it.

`node_id` carries no foreign key. A row outlives its node's registration, and
one that pinned the node would make a node impossible to delete for as long as
mail about it was on record.
"""

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.users import Base

# Where a row is in its life. A queued row is waiting to be sent. A settling one
# has been sent or refused already, and only the handler for its kind is left
# to run. Every other status is final, and names what the mail came to.
QUEUED = "queued"
SETTLING = "settling"
SENT = "sent"
UNDELIVERABLE = "undeliverable"
REJECTED = "rejected"
FAILED = "failed"
EXPIRED = "expired"
CANCELLED = "cancelled"

FINAL_STATUSES = (SENT, UNDELIVERABLE, REJECTED, FAILED, EXPIRED, CANCELLED)


class MailOutbox(Base):
    """One message, from the transaction that queued it to the one that settled it.

    `attempts` counts claims: each time a worker took the row, whether it then
    sent it or settled it without a send. It is also half of what fences a
    worker's result, with `lease_owner`, so a worker whose lease ran out cannot
    record over the worker that took the row from it.

    `outcome` is what delivery came to, written as soon as it is known; `status`
    reaches the same value once the kind's handler has run. The two differ only
    on a settling row.
    """

    __tablename__ = "mail_outbox"
    # AUTOINCREMENT so an id is never reused after a purge: a log line naming
    # a row has to keep naming the same mail.
    __table_args__ = (
        Index("ix_mail_outbox_due", "status", "next_attempt_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    recipient: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    node_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    handle: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)
    next_attempt_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    settled_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
