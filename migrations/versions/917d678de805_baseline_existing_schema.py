"""baseline existing schema

Revision ID: 917d678de805
Revises:
Create Date: 2026-07-13 15:53:22.341824

This used to be `upgrade(): pass`, which meant the schema this whole migration
chain builds on existed nowhere except in the production database, where it had
been created by hand before Alembic was introduced. Nothing in the repository
could produce it: no migration ran `create_table` for `users`, `tasks`,
`clients`, `notifications` or 33 other tables, and there is no `db.create_all()`
anywhere in the application.

The failure mode was quiet, which is what made it dangerous. Restoring onto an
empty database and running `flask db upgrade` did not error - the later
migrations all guard themselves with

    if "some_table" not in inspector.get_table_names(): return

so every one of them skipped, Alembic stamped `alembic_version` at head, and
the result was a database that reported itself fully migrated while containing
only the 16 tables added after the baseline. Two thirds of the application had
no storage and there was nothing in the output to say so.

Why edit an already-applied migration rather than append a new one at head:
on a fresh database the ALTER-style migrations run BEFORE anything appended at
the end, hit their own "table missing" guard, skip, and are never revisited -
so the tables would come into existence without any of the columns those
migrations add. The baseline is the only point in the chain that runs early
enough. Editing it is safe precisely because every existing database is already
stamped past it and will never execute it again; the guard below makes it a
no-op even if one somehow did.

Built from the application's own model metadata rather than 37 hand-written
`op.create_table` calls. A transcription of the models cannot be checked
against them and drifts silently - the drift is how this hole opened in the
first place. Reading the metadata means the baseline cannot disagree with the
models it is supposed to represent, and `create_all` resolves foreign-key
ordering itself.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '917d678de805'
down_revision = None
branch_labels = None
depends_on = None


#: The tables that predate the migration chain - everything the hand-built
#: production schema contained. Listed explicitly rather than "whatever is in
#: the metadata" so that a table added to the models LATER still needs its own
#: migration; otherwise the baseline would quietly absorb new work and the
#: chain would stop describing how the schema got here.
BASELINE_TABLES = (
    "activity_logs",
    "attendance_sessions",
    "attendance_settings",
    "client_deliverables",
    "client_monthly_targets",
    "clients",
    "daily_reports",
    "data_deletion_requests",
    "holidays",
    "leaves",
    "meeting_participants",
    "meetings",
    "note_attachments",
    "note_labels",
    "notes",
    "notifications",
    "permissions",
    "services",
    # NOT social_posting_slots: it has a foreign key to social_accounts, which
    # a later migration creates, so it cannot exist this early. It was added
    # to production by hand after the social tables and gets its own migration
    # at the end of the chain.
    "task_activities",
    "task_comment_reactions",
    "task_comments",
    "task_feedbacks",
    "task_sequences",
    "task_visibility",
    "tasks",
    "teams_channel_members",
    "teams_channels",
    "teams_message_attachments",
    "teams_message_reactions",
    "teams_messages",
    "teams_presence",
    "teams_saved_messages",
    "teams_typing",
    "user_permissions",
    "users",
    "zoho_connections",
)


def upgrade():
    # Imported here, not at module scope: Alembic imports every version file
    # to build the revision map, and that must not depend on the application
    # package being importable.
    from app.extensions import db
    import app.models  # noqa: F401 - registers the models on db.metadata

    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    pending = [
        db.metadata.tables[name]
        for name in BASELINE_TABLES
        if name in db.metadata.tables and name not in existing
    ]
    if not pending:
        # Every real database is already here. This is the path production
        # takes if this migration is ever re-run, and it must do nothing.
        return

    # checkfirst stays on as a second line of defence against a table that
    # exists under a different search_path than the inspector reported.
    db.metadata.create_all(bind=bind, tables=pending, checkfirst=True)


def downgrade():
    # Deliberately empty. Downgrading past the baseline means dropping every
    # table in the application, which is never something a migration should do
    # on the strength of a mistyped revision.
    pass
