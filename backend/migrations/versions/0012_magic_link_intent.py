"""What a sign-in link is for, and which node it is about.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# Two defaulted columns, and every existing row reads "signin", which is what
# they all were. Graded destructive all the same: code from before this revision
# redeems a link by its hash alone, so once claim links share the table it would
# spend one as a sign-in. A rollback across this must be surfaced to a human, and
# the downgrade below is what makes the older code safe to serve.
rollback_safety = "destructive"


def upgrade() -> None:
    # A database built by create_all rather than by the chain already has these
    # columns, and adding one again fails. 0010 takes the same position for the
    # table itself, and 0001 for the four baseline tables: a schema that is
    # already correct makes recording the revision the whole job.
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("magic_links")}
    if "intent" in existing and "node_id" in existing:
        return

    op.add_column("magic_links", sa.Column("intent", sa.String(length=16), server_default="signin", nullable=False))
    op.add_column("magic_links", sa.Column("node_id", sa.String(length=32), nullable=True))
    # Named to match what `index=True` on the model produces, or the schema
    # comparison in tests/test_migrations.py sees two different databases.
    op.create_index("ix_magic_links_intent", "magic_links", ["intent"])


def downgrade() -> None:
    # Drops what every outstanding link is for. A rollback past this leaves the
    # claim links in the table indistinguishable from sign-in links, so they are
    # dropped rather than left to be redeemed as the wrong thing.
    op.execute("DELETE FROM magic_links")
    op.drop_index("ix_magic_links_intent", table_name="magic_links")
    with op.batch_alter_table("magic_links") as batch:
        batch.drop_column("node_id")
        batch.drop_column("intent")
