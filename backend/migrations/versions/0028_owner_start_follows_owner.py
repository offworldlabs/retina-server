"""node_claims.owned_since follows the owner: a start not written for the new
owner goes in the write that changes it.

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

# A trigger, and the clearing of starts nothing reads. Code from before this
# revision names neither. Serving after a rollback, code from 0024 on writes a
# new owner's start in the statement that sets the owner, and the trigger
# keeps it; code from before 0024 sets the owner alone, and the trigger drops
# the last owner's start, which the owner reads already pass over for
# updated_at.
rollback_safety = "additive"

# A copy of core/nodes.py's: a revision imports nothing from the app. The
# parity test in tests/test_migrations.py holds the two to the same statement.
_TRIGGER = """
        CREATE TRIGGER tr_node_claims_owner_start_follows_owner
        AFTER UPDATE OF user_id ON node_claims
        FOR EACH ROW
        WHEN NEW.user_id IS NOT OLD.user_id AND NEW.owned_since_user_id IS NOT NEW.user_id
        BEGIN
            UPDATE node_claims SET owned_since = NULL, owned_since_user_id = NULL WHERE node_id = NEW.node_id;
        END
        """


def upgrade() -> None:
    """Drop every start written for anybody but the claim's owner, and keep it
    so with a trigger.

    A writer that does not know owned_since changes the owner alone and leaves
    the last owner's start on the row: the registry a receiving environment is
    pushed from production, whose node_claims has no such column, or an image
    from before 0024 serving after a rollback. The owner reads pass over a
    start written for somebody else and take updated_at, which that writer
    stamps. The start stays all the same, and when the same kind of write
    hands the node back to the owner it names, it reads as theirs again: they
    are shown the node's records from when they first had it, and with them
    the time somebody else held it.

    The trigger drops the start, owned_since with the owner it names, in the
    statement that changes user_id, unless that statement leaves it naming the
    new owner. Every writer in the app that changes user_id writes the new
    owner's start in that statement: set_owner (services/node_claim_store.py),
    whose rewrite of an owner's id into the account's own spelling rewrites
    owned_since_user_id with it, and complete_claim (services/node_claiming.py).
    Polled radar registration inserts its row, which no update trigger sees.

    Nothing reads the starts cleared here, so no owner is shown anything
    different; left in place, they could come back to life.
    """
    bind = op.get_bind()
    dropped = bind.execute(
        sa.text(
            "UPDATE node_claims SET owned_since = NULL, owned_since_user_id = NULL "
            "WHERE owned_since_user_id IS NOT NULL AND owned_since_user_id IS NOT user_id"
        )
    ).rowcount
    if dropped:
        # start.sh prints what this writes.
        print(f"[0028] {dropped} claim(s) had a start written for somebody other than their owner, now dropped")
    op.execute(_TRIGGER)


def downgrade() -> None:
    # The starts the upgrade dropped stay dropped: nothing read them, and
    # putting them back would only let them come back to life. Without the
    # trigger, a start written for an earlier owner outlives the owner again.
    op.execute("DROP TRIGGER tr_node_claims_owner_start_follows_owner")
