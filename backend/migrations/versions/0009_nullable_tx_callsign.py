"""tx_callsign becomes nullable on node_configs.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# A downgrade cannot express a null, and code predating 1.2.2 has no
# null-handling for this column, so a rollback across this revision must be
# surfaced to a human rather than served as safe. The same grading, for the same
# reason, as 0005 on the coordinates.
rollback_safety = "destructive"


def upgrade() -> None:
    # batch_alter_table because SQLite has no ALTER COLUMN: Alembic copies the
    # table with the corrected definition and swaps it in. Existing rows keep the
    # names they declared; the table is append-only, so this governs new versions
    # only.
    with op.batch_alter_table("node_configs") as batch:
        batch.alter_column("tx_callsign", existing_type=sa.String(length=32), nullable=True)


def downgrade() -> None:
    # Fails, loudly, once any node has registered without a callsign: the table
    # copy hits the NOT NULL and leaves the database stamped at 0009. That is the
    # honest outcome, since the alternative is inventing an illuminator name that
    # nothing downstream could tell from one an owner gave.
    with op.batch_alter_table("node_configs") as batch:
        batch.alter_column("tx_callsign", existing_type=sa.String(length=32), nullable=False)
