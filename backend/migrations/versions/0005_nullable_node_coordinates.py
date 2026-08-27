"""The six coordinate columns become nullable on node_configs.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# A downgrade cannot express a null, and code predating 1.1.3 has no
# null-handling for these six columns, so a rollback across this revision must
# be surfaced to a human rather than served as safe.
rollback_safety = "destructive"

_COLUMNS = ("rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft")


def upgrade() -> None:
    # Existing rows are left exactly as they are. A row that declared (0, 0)
    # stays as declared: the table is append-only, so this governs new rows
    # only, and rewriting history would be guessing at what a node meant.
    with op.batch_alter_table("node_configs") as batch:
        for column in _COLUMNS:
            batch.alter_column(column, existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    # A null cannot be expressed under the old constraint, so a downgrade with
    # positionless rows present will fail loudly rather than invent coordinates.
    with op.batch_alter_table("node_configs") as batch:
        for column in _COLUMNS:
            batch.alter_column(column, existing_type=sa.Float(), nullable=False)
