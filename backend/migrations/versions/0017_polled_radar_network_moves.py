"""How often a polled radar's name has pointed into another network.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

# Two columns with defaults, named by nothing before this revision.
rollback_safety = "additive"


def upgrade() -> None:
    op.add_column("polled_radars", sa.Column("network_moves", sa.Integer(), server_default="0", nullable=False))
    op.add_column("polled_radars", sa.Column("last_network_move_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # SQLite rewrites the table to drop a column, so both go in one pass.
    with op.batch_alter_table("polled_radars") as batch:
        batch.drop_column("last_network_move_at")
        batch.drop_column("network_moves")
