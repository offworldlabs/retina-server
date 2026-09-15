"""Where a node owner's phone number is, as ISO 3166-1 alpha-2.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# One nullable column. Code from before this revision has no query that names
# it, and every existing row reads null, which is what "the owner did not say"
# already meant.
rollback_safety = "additive"


def upgrade() -> None:
    op.add_column("node_contacts", sa.Column("country", sa.String(length=2), nullable=True))


def downgrade() -> None:
    # Drops the column and every code in it. No other column carries the same
    # fact, so a national phone number left behind is dialable only by someone
    # who already knows where it is.
    with op.batch_alter_table("node_contacts") as batch:
        batch.drop_column("country")
