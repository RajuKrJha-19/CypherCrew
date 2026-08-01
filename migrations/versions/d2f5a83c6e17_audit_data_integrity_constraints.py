"""audit: data-integrity constraints

Revision ID: d2f5a83c6e17
Revises: c1e4f27a9b30
Create Date: 2026-08-01

Four invariants the application relied on but the database did not enforce.
Every one of them is currently true of the production data - verified before
writing this - so these constraints add no cleanup, only a floor.

  * daily_reports had no unique key on (employee_id, report_date). The route
    get-or-creates by that pair, so a double-submitted "Add report" raced into
    two rows for one employee-day, and the timesheet then listed the day twice
    and doubled its completed count.

  * client_deliverables.completed_count / target_count were nullable with a
    Python-side default and no lower bound. The max(0, ...) clamp lived in two
    routes; anything writing outside them could store NULL or a negative, and
    the client dashboard coalesces NULL to 0 - so a negative drift rendered as
    if the counter were merely behind.

  * publish_jobs.idempotency_key was `unique=True` but nullable, and Postgres
    permits unlimited NULLs in a unique index. The class docstring promises a
    target is never published twice for the same schedule; that promise did
    not hold for a job created without a key. Both creation sites do set one
    today, which is why this is safe to make NOT NULL now.

  * tasks.task_code was likewise unique-but-nullable, and R2 object keys are
    built from it - two NULL codes would fall back and collide.

Guarded like the rest of the chain: each step checks for itself, so this is a
no-op on a database that already has it.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd2f5a83c6e17'
down_revision = 'c1e4f27a9b30'
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _constraint_names(table):
    insp = _inspector()
    names = {c["name"] for c in insp.get_unique_constraints(table)}
    names |= {c.get("name") for c in insp.get_check_constraints(table)}
    names |= {i["name"] for i in insp.get_indexes(table)}
    return {n for n in names if n}


def upgrade():
    # -- daily_reports: one report per person per day --------------------
    if _has_table("daily_reports"):
        if "uq_daily_report_employee_date" not in _constraint_names(
                "daily_reports"):
            # Collapse any duplicates first. There are none in production, but
            # a constraint that fails on someone else's database is worse than
            # one that tidies up: keep the earliest row for each pair.
            op.execute("""
                DELETE FROM daily_reports a
                USING daily_reports b
                WHERE a.employee_id = b.employee_id
                  AND a.report_date = b.report_date
                  AND a.id > b.id
            """)
            op.create_unique_constraint(
                "uq_daily_report_employee_date", "daily_reports",
                ["employee_id", "report_date"])

    # -- client_deliverables: the counters cannot be NULL or negative ----
    if _has_table("client_deliverables"):
        op.execute(
            "UPDATE client_deliverables SET completed_count = 0 "
            "WHERE completed_count IS NULL OR completed_count < 0")
        op.execute(
            "UPDATE client_deliverables SET target_count = 0 "
            "WHERE target_count IS NULL OR target_count < 0")

        for column in ("completed_count", "target_count"):
            op.alter_column("client_deliverables", column,
                            existing_type=sa.Integer(),
                            nullable=False, server_default="0")

        if "ck_client_deliverables_counts_non_negative" not in \
                _constraint_names("client_deliverables"):
            op.create_check_constraint(
                "ck_client_deliverables_counts_non_negative",
                "client_deliverables",
                "completed_count >= 0 AND target_count >= 0")

    # -- publish_jobs: the idempotency guarantee needs the key ----------
    if _has_table("publish_jobs"):
        columns = {c["name"]: c for c in _inspector().get_columns(
            "publish_jobs")}
        if columns.get("idempotency_key", {}).get("nullable", True):
            # A NULL key means the job predates the guarantee; there are none,
            # but give any that exist a unique value rather than dropping them.
            op.execute(
                "UPDATE publish_jobs SET idempotency_key = "
                "'legacy-' || id::text WHERE idempotency_key IS NULL")
            op.alter_column("publish_jobs", "idempotency_key",
                            existing_type=sa.String(length=255),
                            nullable=False)

    # -- tasks.task_code ------------------------------------------------
    if _has_table("tasks"):
        columns = {c["name"]: c for c in _inspector().get_columns("tasks")}
        if columns.get("task_code", {}).get("nullable", True):
            # Nothing to backfill in production. A row without a code cannot be
            # given a meaningful one here (the sequence lives in the app), so
            # this only tightens a column that is already always populated.
            if op.get_bind().execute(sa.text(
                    "SELECT count(*) FROM tasks WHERE task_code IS NULL"
            )).scalar() == 0:
                # INTEGER, not a string - the column is a sequence number and
                # the "TASK-<code>" form is built at render time. Passing the
                # wrong existing_type here would ask Postgres to rewrite the
                # column rather than just tighten it.
                op.alter_column("tasks", "task_code",
                                existing_type=sa.Integer(),
                                nullable=False)


def downgrade():
    if _has_table("tasks"):
        op.alter_column("tasks", "task_code",
                        existing_type=sa.String(length=50), nullable=True)

    if _has_table("publish_jobs"):
        op.alter_column("publish_jobs", "idempotency_key",
                        existing_type=sa.String(length=255), nullable=True)

    if _has_table("client_deliverables"):
        if "ck_client_deliverables_counts_non_negative" in _constraint_names(
                "client_deliverables"):
            op.drop_constraint("ck_client_deliverables_counts_non_negative",
                               "client_deliverables", type_="check")
        for column in ("completed_count", "target_count"):
            op.alter_column("client_deliverables", column,
                            existing_type=sa.Integer(), nullable=True,
                            server_default=None)

    if _has_table("daily_reports"):
        if "uq_daily_report_employee_date" in _constraint_names(
                "daily_reports"):
            op.drop_constraint("uq_daily_report_employee_date",
                               "daily_reports", type_="unique")
