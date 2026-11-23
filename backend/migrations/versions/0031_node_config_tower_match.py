"""The licensed tower the server matched a node's configured illuminator to: its
callsign, EIRP, antenna height, band and polarisation, how the match came out,
and when it was made.

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

# Seven nullable columns with no default. Code from before this revision has
# no query that names them, and every existing row reads null, which is what
# "not matched yet" means.
rollback_safety = "additive"


def upgrade() -> None:
    op.add_column("node_configs", sa.Column("tower_callsign", sa.String(length=32), nullable=True))
    op.add_column("node_configs", sa.Column("tower_eirp_dbm", sa.Float(), nullable=True))
    op.add_column("node_configs", sa.Column("tower_antenna_height_m", sa.Float(), nullable=True))
    op.add_column("node_configs", sa.Column("tower_band", sa.String(length=16), nullable=True))
    op.add_column("node_configs", sa.Column("tower_polarisation", sa.String(length=16), nullable=True))
    op.add_column("node_configs", sa.Column("tower_match_status", sa.String(length=24), nullable=True))
    op.add_column("node_configs", sa.Column("tower_matched_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # SQLite rewrites the table to drop a column, so all seven go in one pass.
    # Nothing here is the node's own word: the server derived every value, and
    # the tower match derives them again once the columns are back.
    with op.batch_alter_table("node_configs") as batch:
        batch.drop_column("tower_matched_at")
        batch.drop_column("tower_match_status")
        batch.drop_column("tower_polarisation")
        batch.drop_column("tower_band")
        batch.drop_column("tower_antenna_height_m")
        batch.drop_column("tower_eirp_dbm")
        batch.drop_column("tower_callsign")
