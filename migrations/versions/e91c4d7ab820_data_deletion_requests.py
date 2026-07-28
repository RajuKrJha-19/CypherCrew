"""Record of user-data deletion requests.

Meta requires an app that touches platform user data to accept deletion
requests and hand back a status URL plus a confirmation code. That means
the request has to outlive the HTTP call, so it gets a table.

Additive only - one new table, nothing existing is touched. Guarded so it
is safe to re-run.

Revision ID: e91c4d7ab820
Revises: d7a24c8b91e3
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e91c4d7ab820"
down_revision = "d7a24c8b91e3"
branch_labels = None
depends_on = None

TABLE = "data_deletion_requests"


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if TABLE in inspector.get_table_names():
        return

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        # Unique: it is the handle the requester quotes back to us, and the
        # only thing standing between the status page and enumeration.
        sa.Column("confirmation_code", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("platform", sa.String(length=30), nullable=True),
        sa.Column("external_user_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False,
                  server_default="received"),
        sa.Column("deleted", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("confirmation_code",
                            name="uq_data_deletion_confirmation_code"),
    )
    op.create_index("ix_data_deletion_requests_confirmation_code", TABLE,
                    ["confirmation_code"])
    op.create_index("ix_data_deletion_requests_external_user_id", TABLE,
                    ["external_user_id"])
    op.create_index("ix_data_deletion_requests_status", TABLE, ["status"])


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return
    op.drop_table(TABLE)
