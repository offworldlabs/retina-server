"""node_location_privacy: the location choice made outside registration.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# One new table and nothing else. Code from before this revision has no query
# that names it, and the precedence rule it feeds degrades to exactly what that
# code already computed — an absent override is the registration choice — so an
# older image serving against this schema publishes what it always published.
rollback_safety = "additive"


def upgrade() -> None:
    # No foreign key to `nodes`, and String(255) rather than that table's
    # String(32): the point of the table is to carry a choice for ids that never
    # registered — the synthetic fleet, mirrored nodes, anything predating the
    # v1 handshake — so a constraint against `nodes` would refuse the rows it
    # exists to hold. Same key space as node_owners.node_id, which is what the
    # owner routes join it against. See core/nodes.NodeLocationPrivacy.
    #
    # No index beyond the primary key: every read is either by node_id or a full
    # scan for the 30 s cache refresh in services/publication.py, and the table
    # holds one row per node that has made a choice.
    op.create_table(
        "node_location_privacy",
        sa.Column("node_id", sa.String(length=255), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.Column("set_by", sa.String(length=255), server_default="", nullable=False),
        sa.Column("set_at", sa.Float(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("node_id"),
    )


def downgrade() -> None:
    # Dropping the table discards every override, which returns each node to its
    # registration choice. That is a real loss of intent — a node an owner hid
    # from the dashboard becomes public again — but there is nowhere else to put
    # it, and the alternative of refusing the downgrade would strand a rollback
    # on a revision that is otherwise additive. Operators rolling back across
    # this revision should expect to re-apply the choices.
    op.drop_table("node_location_privacy")
