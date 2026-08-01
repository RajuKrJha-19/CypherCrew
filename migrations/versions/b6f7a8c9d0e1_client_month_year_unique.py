"""Clients: one monthly-target row per (client, month, year).

The add-deliverable path get-or-creates a ClientMonthlyTarget for
(client_id, month, year); two near-simultaneous submits could each miss
the other's row and create a duplicate, splitting that month's
deliverables across two targets so the dashboard tally reads low. This
collapses any existing duplicates (keeping the earliest row and moving
its siblings' deliverables onto it) and adds the unique constraint that
makes the split impossible.

Revision ID: b6f7a8c9d0e1
Revises: a5e6f7b8c9d0
"""

import sqlalchemy as sa
from alembic import op

revision = "b6f7a8c9d0e1"
down_revision = "a5e6f7b8c9d0"
branch_labels = None
depends_on = None

TABLE = "client_monthly_targets"
CONSTRAINT = "uq_client_month_year"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    existing = {c["name"] for c in inspector.get_unique_constraints(TABLE)}
    existing |= {i["name"] for i in inspector.get_indexes(TABLE)}
    if CONSTRAINT in existing:
        return

    # 1. Re-point every deliverable hanging off a duplicate target onto the
    #    earliest (lowest-id) target for that (client, month, year).
    op.execute(sa.text("""
        UPDATE client_deliverables d
        SET monthly_target_id = keep.min_id
        FROM client_monthly_targets t
        JOIN (
            SELECT client_id, month, year, MIN(id) AS min_id
            FROM client_monthly_targets
            GROUP BY client_id, month, year
        ) keep
          ON t.client_id = keep.client_id
         AND t.month = keep.month
         AND t.year = keep.year
        WHERE d.monthly_target_id = t.id
          AND t.id <> keep.min_id
    """))

    # 2. Delete the now-empty duplicate targets.
    op.execute(sa.text("""
        DELETE FROM client_monthly_targets t
        USING (
            SELECT client_id, month, year, MIN(id) AS min_id
            FROM client_monthly_targets
            GROUP BY client_id, month, year
        ) keep
        WHERE t.client_id = keep.client_id
          AND t.month = keep.month
          AND t.year = keep.year
          AND t.id <> keep.min_id
    """))

    # 3. Now the column set is unique - enforce it.
    op.create_unique_constraint(
        CONSTRAINT, TABLE, ["client_id", "month", "year"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    existing = {c["name"] for c in inspector.get_unique_constraints(TABLE)}
    if CONSTRAINT in existing:
        op.drop_constraint(CONSTRAINT, TABLE, type_="unique")
