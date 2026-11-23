"""mail_outbox and rate_counters: mail waiting to be sent, and the counters
that limit how often it may be asked for.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

# Two new tables. Code from before this revision has no queries that name
# either of them.
rollback_safety = "additive"


def upgrade() -> None:
    op.create_table(
        "mail_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("recipient", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("node_id", sa.String(length=32), nullable=True),
        sa.Column("handle", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("next_attempt_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("settled_at", sa.Float(), nullable=True),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.Float(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("message_id", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_mail_outbox_due", "mail_outbox", ["status", "next_attempt_at"])
    op.create_index("ix_mail_outbox_node_id", "mail_outbox", ["node_id"])

    op.create_table(
        "rate_counters",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("window_s", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.Float(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("key", "window_s"),
    )
    op.create_index("ix_rate_counters_expires_at", "rate_counters", ["expires_at"])


def downgrade() -> None:
    # Drops whatever mail is still waiting to go, and every counter.
    op.drop_index("ix_rate_counters_expires_at", table_name="rate_counters")
    op.drop_table("rate_counters")
    op.drop_index("ix_mail_outbox_node_id", table_name="mail_outbox")
    op.drop_index("ix_mail_outbox_due", table_name="mail_outbox")
    op.drop_table("mail_outbox")
