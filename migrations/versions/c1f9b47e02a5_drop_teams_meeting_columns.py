"""Drop the meeting columns Cypher-Teams added.

Teams no longer does meetings: the Jitsi adapter, the provider registry,
the scheduling UI and the in-channel call are gone, and `/meetings` is back
to being the diary page it was before. These eight columns had exactly one
reader between them, and it no longer exists.

Destructive on purpose - the alternative is eight dead columns nobody can
explain in a year. Nothing is lost that matters: `room_key` and `provider`
only ever meant "which Jitsi room", and `started_at`/`ended_at` only ever
recorded a call that can no longer be started. The diary columns the old
module actually uses - title, client_id, meeting_date, agenda, created_at
and the participants table - are untouched.

The downgrade restores the columns but not their contents, which is the
honest shape of undoing a drop.

Revision ID: c1f9b47e02a5
Revises: b8e5d3a91c47
"""

import sqlalchemy as sa
from alembic import op

revision = "c1f9b47e02a5"
down_revision = "b8e5d3a91c47"
branch_labels = None
depends_on = None

TABLE = "meetings"

#: Dropped in this order; constraints first, then the columns they name.
CONSTRAINTS = (
    ("uq_meetings_room_key", "unique"),
    ("fk_meetings_created_by", "foreignkey"),
    ("fk_meetings_channel", "foreignkey"),
)

COLUMNS = (
    ("room_key", sa.String(length=64), {"nullable": True}),
    ("duration_minutes", sa.Integer(),
     {"nullable": False, "server_default": "30"}),
    ("status", sa.String(length=20),
     {"nullable": False, "server_default": "scheduled"}),
    ("created_by_id", sa.Integer(), {"nullable": True}),
    ("channel_id", sa.Integer(), {"nullable": True}),
    ("provider", sa.String(length=30),
     {"nullable": False, "server_default": "jitsi"}),
    ("started_at", sa.DateTime(), {"nullable": True}),
    ("ended_at", sa.DateTime(), {"nullable": True}),
)


def _columns(inspector):
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        return

    existing_unique = {c["name"] for c in inspector.get_unique_constraints(TABLE)}
    existing_fk = {c["name"] for c in inspector.get_foreign_keys(TABLE)}

    for name, kind in CONSTRAINTS:
        present = existing_unique if kind == "unique" else existing_fk
        if name in present:
            op.drop_constraint(name, TABLE, type_=kind)

    present = _columns(inspector)
    for name, _type, _kwargs in COLUMNS:
        if name in present:
            op.drop_column(TABLE, name)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        return

    present = _columns(inspector)
    for name, type_, kwargs in COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, type_, **kwargs))

    inspector = sa.inspect(bind)
    present = _columns(inspector)

    if "room_key" in present:
        op.create_unique_constraint("uq_meetings_room_key", TABLE, ["room_key"])
    if "created_by_id" in present:
        op.create_foreign_key("fk_meetings_created_by", TABLE, "users",
                              ["created_by_id"], ["id"])
    # teams_channels may itself be gone if Teams was rolled back further;
    # the FK is only restored when there is something to point at.
    if "channel_id" in present and "teams_channels" in inspector.get_table_names():
        op.create_foreign_key("fk_meetings_channel", TABLE, "teams_channels",
                              ["channel_id"], ["id"], ondelete="SET NULL")
