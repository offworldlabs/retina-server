"""node_events: the durable record of human decisions about a node.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

# One new table. Code from before this revision has no queries that name it.
rollback_safety = "additive"


def upgrade() -> None:
    op.create_table(
        "node_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.node_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_node_events_node_id", "node_events", ["node_id"])


def downgrade() -> None:
    # Drops the record of every decision taken so far.
    op.drop_index("ix_node_events_node_id", table_name="node_events")
    op.drop_table("node_events")
