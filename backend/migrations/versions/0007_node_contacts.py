"""Owner contact details, one mutable row per node.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# One new table and nothing else. Code from before this revision has no queries
# that name it, as 0002 says of its three.
rollback_safety = "additive"


def upgrade() -> None:
    op.create_table(
        "node_contacts",
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("first_name", sa.String(length=64), nullable=True),
        sa.Column("last_name", sa.String(length=64), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.node_id"]),
        sa.PrimaryKeyConstraint("node_id"),
    )


def downgrade() -> None:
    # Drops the table and every contact row in it. The deploy path never
    # downgrades, but a manual `alembic downgrade` here destroys owner contact
    # details irrecoverably.
    op.drop_table("node_contacts")
