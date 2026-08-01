"""Can this repository rebuild its own database?

For most of the app's life the answer was no, and nothing said so. The baseline
migration was `upgrade(): pass`, no migration ran `create_table` for `users`,
`tasks`, `clients` or 34 other tables, and there is no `db.create_all()`
anywhere. A restore onto an empty database ran to completion, stamped
`alembic_version` at head, and produced a schema containing 16 of 54 tables -
because every later migration guards itself with "if the table is missing,
return", so they all skipped in silence.

That is the worst shape a data-loss risk can take: invisible until the day it
matters, and reporting success while it happens.

These tests are static - they read the models and the migration files rather
than building a database, so they run anywhere. What they pin is the invariant
that broke: **every table the application defines must be created by the
migration chain.** A model added later with no migration fails here rather than
on the day someone restores a backup.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "migrations" / "versions"
BASELINE = VERSIONS / "917d678de805_baseline_existing_schema.py"


def _baseline_tables():
    """BASELINE_TABLES, read without importing the migration (which pulls in
    Alembic's op context)."""
    tree = ast.parse(BASELINE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "BASELINE_TABLES":
                    return set(ast.literal_eval(node.value))
    raise AssertionError("BASELINE_TABLES is gone from the baseline migration")


def _created_by_migrations():
    created = set()
    for f in VERSIONS.glob("*.py"):
        src = f.read_text(encoding="utf-8", errors="ignore")
        created |= set(re.findall(
            r"create_table\(\s*[\"']([^\"']+)[\"']", src))
    return created


def _model_tables(app):
    from app.extensions import db

    with app.app_context():
        return set(db.metadata.tables)


# ----------------------------------------------------------------------
# The invariant
# ----------------------------------------------------------------------

def test_every_model_table_is_created_by_the_chain(app):
    """The one that would have caught the original hole."""
    covered = _baseline_tables() | _created_by_migrations()
    missing = sorted(_model_tables(app) - covered)

    assert not missing, (
        "%d table(s) exist in the models but no migration creates them, so a "
        "database restored from empty would silently lack them: %s"
        % (len(missing), ", ".join(missing))
    )


def test_the_baseline_actually_does_something():
    """It was `pass` for the app's entire history."""
    src = BASELINE.read_text(encoding="utf-8")
    body = src.split("def upgrade():")[1].split("def downgrade():")[0]

    assert "create_all" in body
    assert body.strip() != "pass"


def test_the_baseline_only_claims_tables_that_exist_in_the_models(app):
    """A name left behind after a model was renamed would be created as
    nothing and mask a real gap."""
    stale = sorted(_baseline_tables() - _model_tables(app))

    assert not stale, "baseline names tables the models no longer define: %s" % stale


def test_the_baseline_is_guarded():
    """Editing an applied migration is only safe because it cannot act on a
    database that already has the tables."""
    body = BASELINE.read_text(encoding="utf-8").split("def upgrade():")[1]

    assert "get_table_names()" in body
    assert "checkfirst=True" in body


def test_the_baseline_does_not_claim_social_posting_slots():
    """It has a foreign key to social_accounts, which a later migration
    creates - putting it in the baseline makes a fresh upgrade fail outright."""
    assert "social_posting_slots" not in _baseline_tables()


# ----------------------------------------------------------------------
# The columns that existed nowhere but production
# ----------------------------------------------------------------------

@pytest.mark.parametrize("column", ["status_changed_at", "status_started_at"])
def test_task_status_columns_are_covered(app, column):
    """Declared on the model, present in production, in no migration. On a
    rebuilt database every INSERT INTO tasks would have failed."""
    from app.models import Task

    assert column in Task.__table__.columns, (
        "%s is gone from the model - drop this test with it" % column
    )
    assert "tasks" in _baseline_tables(), (
        "tasks is no longer created by the baseline, so %s is uncovered again"
        % column
    )


# ----------------------------------------------------------------------
# Chain shape
# ----------------------------------------------------------------------

def _revisions():
    out = {}
    for f in VERSIONS.glob("*.py"):
        src = f.read_text(encoding="utf-8", errors="ignore")
        rev = re.search(r"^revision = ['\"]([^'\"]+)['\"]", src, re.M)
        down = re.search(r"^down_revision = (?:['\"]([^'\"]+)['\"]|None)",
                         src, re.M)
        if rev:
            out[rev.group(1)] = down.group(1) if down else None
    return out


def test_one_root_and_one_head():
    revs = _revisions()
    roots = [r for r, d in revs.items() if d is None]
    heads = set(revs) - {d for d in revs.values() if d}

    assert roots == ["917d678de805"], "expected the baseline to be the only root"
    assert len(heads) == 1, "diverging heads: %s" % sorted(heads)


def test_no_migration_points_at_a_missing_revision():
    revs = _revisions()
    for rev, down in revs.items():
        assert down is None or down in revs, (
            "%s points at %s, which no file defines" % (rev, down))


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------

@pytest.mark.parametrize("revision,table,column", [
    ("2cb76f30550a", "users", "avatar_key"),
    ("4908547106d8", "clients", "short_code"),
    ("54218146054f", "tasks", "is_social_media"),
    ("7ae8edf352c4", "tasks", "on_hold_seconds"),
    ("ccb2b2dce4a8", "tasks", "backup_assignee_id"),
    ("e5dda5583e93", "clients", "parent_client_id"),
    ("f8f1b17419fd", "notifications", "category"),
])
def test_migrations_that_alter_baseline_tables_are_idempotent(
        revision, table, column):
    """Now that the baseline builds these tables in their current shape, a
    fresh upgrade reaches these migrations with the columns already present.
    Each one uses batch_alter_table, which cannot guard its own statements, so
    the guard has to be an early return at the top."""
    path = next(VERSIONS.glob(revision + "*.py"))
    body = path.read_text(encoding="utf-8").split("def upgrade():")[1]
    body = body.split("def downgrade():")[0]

    assert 'get_columns("%s")' % table in body, (
        "%s does not check whether %s.%s is already there, so a rebuild from "
        "empty fails with DuplicateColumn" % (path.name, table, column)
    )
    assert '"%s" in {' % column in body
