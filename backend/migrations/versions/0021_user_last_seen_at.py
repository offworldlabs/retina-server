"""When an account was last seen: a sign-in, or any use of its session.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

# One nullable column. Code from before this revision has no query that names
# it, and every existing row reads null, which the users page shows as no
# visit on record.
rollback_safety = "additive"


def upgrade() -> None:
    op.add_column("user", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user") as batch:
        batch.drop_column("last_seen_at")
