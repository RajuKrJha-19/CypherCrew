"""add performance indexes

Adds indexes on the hot filter / sort / foreign-key / join columns the
performance audit found unindexed. These tables were created outside
Alembic (the baseline was adopted, not built), so Postgres has no index on
them unless one was hand-created - and Postgres never auto-indexes foreign
keys. Every dashboard, the task board, the review queue and the 5-12s
polling all filter on these columns; unindexed they are sequential scans
that get slower as data grows.

Purely additive and backward compatible: only CREATE INDEX, no schema or
data change. Built CONCURRENTLY so it never takes a write-blocking lock on
a live table, and IF NOT EXISTS so it is a safe no-op wherever an index was
already created by hand.

Revision ID: 55cf46b91e88
Revises: 2cb76f30550a
Create Date: 2026-07-25 02:20:32.541068

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '55cf46b91e88'
down_revision = '2cb76f30550a'
branch_labels = None
depends_on = None


# (index name, table, column expression)
INDEXES = [
    ("ix_tasks_status", "tasks", "status"),
    ("ix_tasks_assigned_to_id", "tasks", "assigned_to_id"),
    ("ix_tasks_client_id", "tasks", "client_id"),
    ("ix_tasks_created_by_id", "tasks", "created_by_id"),
    ("ix_tasks_deliverable_id", "tasks", "deliverable_id"),
    ("ix_tasks_deadline", "tasks", "deadline"),
    ("ix_tasks_created_at", "tasks", "created_at"),
    # Composite: the notifications widget polls "my unread count" and "my
    # recent notifications" every few seconds for every logged-in user.
    ("ix_notifications_user_id_is_read", "notifications", "user_id, is_read"),
    ("ix_notifications_user_id_id", "notifications", "user_id, id"),
    # visible_to membership test in the task/gallery visibility scope.
    ("ix_task_visibility_user_id", "task_visibility", "user_id"),
]


def upgrade():
    # CONCURRENTLY + IF NOT EXISTS require running outside Alembic's
    # per-migration transaction.
    with op.get_context().autocommit_block():
        # An index build must never be cut short by the app's statement
        # timeout (set on the shared engine); clear it for this connection.
        op.execute("SET statement_timeout = 0")
        for name, table, cols in INDEXES:
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                f"ON {table} ({cols})"
            )


def downgrade():
    with op.get_context().autocommit_block():
        for name, _table, _cols in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
