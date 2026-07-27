"""Role catalog + permission integrity

Widens users.role for the new job roles, indexes it, and gives
user_permissions the uniqueness and the audit trail it never had.

Written by hand, defensively, for one reason: `users` and
`user_permissions` predate Alembic entirely. The root revision
(917d678de805) is a stamp-only no-op, so the migration chain has never
described these tables and cannot recreate them - whatever shape the live
database has is the shape somebody created by hand. So every step here
inspects first and does nothing if the change is already present, which
also makes the whole revision safe to re-run.

Autogenerate was not used and should not be: earlier revisions in this
tree note that it proposes dropping indexes it does not recognise.

Revision ID: c3f7a91b60d4
Revises: a15ab31540d7
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "c3f7a91b60d4"
down_revision = "a15ab31540d7"
branch_labels = None
depends_on = None


UQ_NAME = "uq_user_permissions_user_permission"
FK_GRANTED_BY = "fk_user_permissions_granted_by_id_users"
IX_ROLE = "ix_users_role"


def _inspector():
    return inspect(op.get_bind())


def _role_length(insp):
    """Current length of users.role, or None if it cannot be read."""
    for column in insp.get_columns("users"):
        if column["name"] == "role":
            return getattr(column["type"], "length", None)
    return None


def upgrade():
    insp = _inspector()
    tables = set(insp.get_table_names())

    # -- users.role: 30 -> 50 ------------------------------------------
    # The longest role value (senior_social_media_executive) is 29
    # characters. Postgres errors rather than truncates on overflow, and it
    # would only error the first time somebody was actually given that
    # role - in production, long after this merged.
    if "users" in tables:
        length = _role_length(insp)

        if length is not None and length < 50:
            with op.batch_alter_table("users", schema=None) as batch_op:
                batch_op.alter_column(
                    "role",
                    existing_type=sa.String(length=length),
                    type_=sa.String(length=50),
                    existing_nullable=False,
                )

        # The five team-dashboard queries and eleven people-pickers all
        # filter on role, and it now has fifteen distinct values instead
        # of three.
        if IX_ROLE not in {i["name"] for i in insp.get_indexes("users")}:
            op.create_index(IX_ROLE, "users", ["role"], unique=False)

    if "user_permissions" not in tables:
        return

    # -- user_permissions: dedupe, then constrain ----------------------
    existing_uniques = {
        c["name"] for c in insp.get_unique_constraints("user_permissions")
    }

    if UQ_NAME not in existing_uniques:
        # Must precede the constraint or it fails on any database where a
        # double-submitted permissions form wrote the same grant twice.
        # Keeping the lowest id keeps the earliest grant.
        op.execute(sa.text(
            """
            DELETE FROM user_permissions
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM user_permissions
                GROUP BY user_id, permission_id
            )
            """
        ))

        with op.batch_alter_table("user_permissions", schema=None) as batch_op:
            batch_op.create_unique_constraint(
                UQ_NAME, ["user_id", "permission_id"]
            )

    # -- user_permissions: who granted it, and when --------------------
    columns = {c["name"] for c in insp.get_columns("user_permissions")}

    with op.batch_alter_table("user_permissions", schema=None) as batch_op:

        if "granted_at" not in columns:
            # Nullable: rows that predate this column have no honest value.
            batch_op.add_column(
                sa.Column("granted_at", sa.DateTime(), nullable=True)
            )

        if "granted_by_id" not in columns:
            batch_op.add_column(
                sa.Column("granted_by_id", sa.Integer(), nullable=True)
            )
            batch_op.create_foreign_key(
                FK_GRANTED_BY, "users", ["granted_by_id"], ["id"]
            )


def downgrade():
    """Reverses the schema changes.

    Note the role truncation below: narrowing the column back to 30 would
    fail outright on any row holding one of the 29-character role values
    once they exist, so anyone on a new role is reset to `employee` first.
    That is data loss, and it is the honest behaviour - the old schema
    genuinely cannot hold those values.
    """
    insp = _inspector()
    tables = set(insp.get_table_names())

    if "user_permissions" in tables:
        columns = {c["name"] for c in insp.get_columns("user_permissions")}

        with op.batch_alter_table("user_permissions", schema=None) as batch_op:

            if "granted_by_id" in columns:
                batch_op.drop_constraint(FK_GRANTED_BY, type_="foreignkey")
                batch_op.drop_column("granted_by_id")

            if "granted_at" in columns:
                batch_op.drop_column("granted_at")

        uniques = {
            c["name"] for c in insp.get_unique_constraints("user_permissions")
        }
        if UQ_NAME in uniques:
            with op.batch_alter_table("user_permissions", schema=None) as batch_op:
                batch_op.drop_constraint(UQ_NAME, type_="unique")

    if "users" in tables:

        if IX_ROLE in {i["name"] for i in insp.get_indexes("users")}:
            op.drop_index(IX_ROLE, table_name="users")

        op.execute(sa.text(
            "UPDATE users SET role = 'employee' WHERE length(role) > 30"
        ))

        with op.batch_alter_table("users", schema=None) as batch_op:
            batch_op.alter_column(
                "role",
                existing_type=sa.String(length=50),
                type_=sa.String(length=30),
                existing_nullable=False,
            )
