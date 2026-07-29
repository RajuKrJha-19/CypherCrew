"""Pytest fixtures for the Social Publishing Engine.

The engine is provider-agnostic, so the whole suite runs against a
FakeProvider (registered as platform "fake") - no network, no credentials,
no real platform. Tests exercise the queue/state-machine, retry engine,
rate gate, scheduler, recovery and status exactly as a real adapter would
drive them.

Isolation: the engine owns dedicated tables (social_*, publish_*, etc.),
so each test truncates only those tables before and after it runs. No
business-domain table is ever touched.

The env vars below must be set BEFORE `app` is imported (config reads them
at import time), which is why they're at module top.
"""

import os
from datetime import datetime, timedelta

from cryptography.fernet import Fernet

os.environ.setdefault("SOCIAL_ENGINE_ENABLED", "true")
os.environ.setdefault("SOCIAL_TOKEN_KEY", Fernet.generate_key().decode())
os.environ.setdefault("SOCIAL_WORKER_TOKEN", "test-worker-token")
os.environ.setdefault("AUTO_SEED", "false")
# Tests drive the worker explicitly; the background auto-worker must stay off.
os.environ.setdefault("SOCIAL_INPROCESS_WORKER", "false")
# The suite asserts the pure SimulationProvider is registered for every
# platform, so pin simulation mode and force the Graph emulator OFF - otherwise
# a developer's .env (META_EMULATOR / META_APP_ID) would flip facebook +
# instagram to the real adapter and break the simulation tests. The emulator
# path is covered separately by the qa.py harness. Set (not setdefault) so an
# ambient .env value can't win. load_dotenv (override=False) won't clobber these.
os.environ["META_EMULATOR"] = "false"
os.environ.pop("META_APP_ID", None)
os.environ.setdefault("SOCIAL_SIMULATION_MODE", "true")
# Cypher-Teams. On for the suite so its blueprint is registered and the
# routes are reachable; set (like the Meta vars above) BEFORE the app is
# imported, because config reads the environment at import time.
os.environ.setdefault("TEAMS_ENABLED", "true")

import pytest  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db as _db  # noqa: E402
from app.social.registry import registry  # noqa: E402
from app.social.providers.base import SocialProvider  # noqa: E402
from app.social.dto import (  # noqa: E402
    AccountInfo, Capabilities, PublishStep, TokenBundle,
)
from app.social.errors import (  # noqa: E402
    AuthError, PermanentError, TransientError,
)


class FakeProvider(SocialProvider):
    """A fully in-memory provider. `mode` (class attr, reset to 'ok' per
    test) selects the behaviour of start_publish:
      ok        -> DONE
      pending   -> PENDING (poll_publish then returns DONE)
      transient -> raises, mapped to TransientError
      auth      -> raises, mapped to AuthError
      permanent -> raises, mapped to PermanentError
    """

    key = "fake"
    capabilities = Capabilities(
        post_types={"image", "carousel", "video"},
        publish_rate=(100, "24h"),
        max_carousel=10,
        supports_first_comment=True,
    )
    mode = "ok"
    comments = []  # (external_post_id, text) recorded by post_first_comment

    def build_oauth_url(self, state, redirect_uri):
        return f"https://fake.test/auth?state={state}"

    def exchange_code(self, code, code_verifier, redirect_uri):
        return TokenBundle(access_token="AT", scopes="fake_publish")

    def list_publishable_accounts(self, token):
        return [AccountInfo("EXT1", "Fake Page", "page")]

    def validate(self, content):
        return []

    def start_publish(self, target, content, token):
        m = FakeProvider.mode
        if m == "transient":
            raise RuntimeError("transient boom")
        if m == "auth":
            raise RuntimeError("401 invalid token")
        if m == "permanent":
            raise RuntimeError("400 bad request")
        if m == "pending":
            return PublishStep(status="pending", provider_state={"container": "C1"})
        return PublishStep(status="done", external_post_id="EXT_POST_1",
                           permalink="https://fake.test/p/1")

    def poll_publish(self, target, provider_state, token):
        return PublishStep(status="done", external_post_id="EXT_POST_1",
                           permalink="https://fake.test/p/1")

    def fetch_analytics(self, target, token):
        return {"likes": 3, "reach": 10}

    def post_first_comment(self, external_post_id, text, token):
        FakeProvider.comments.append((external_post_id, text))
        return "CMT1"

    def map_error(self, exc):
        m = FakeProvider.mode
        s = str(exc)
        if m == "transient":
            return TransientError(s)
        if m == "auth":
            return AuthError(s)
        return PermanentError(s)


def _social_models():
    """Ordered child-first, so the bulk deletes below don't trip an FK.

    Anything a Studio test can create belongs here - including
    DataDeletionRequest, which the deletion-callback tests write and which
    would otherwise pile up in the shared dev database run after run.
    """
    from app.models import (
        PublishResult, PublishJob, SocialMediaAsset, SocialAnalyticsSnapshot,
        PlatformRateBudget, SocialAuditLog, ContentVersion, SocialPostTarget,
        SocialPost, SocialOAuthState, SocialAccount, SocialComment,
        DataDeletionRequest,
    )
    return [
        PublishResult, PublishJob, SocialComment, SocialMediaAsset,
        SocialAnalyticsSnapshot, PlatformRateBudget, SocialAuditLog,
        ContentVersion, SocialPostTarget, SocialPost, SocialOAuthState,
        SocialAccount, DataDeletionRequest,
    ]


def _teams_models():
    """Ordered child-first, same contract as _social_models.

    Cypher-Teams owns its own teams_* tables, so a test can wipe all of
    them without touching a single business-domain row. `meetings` is
    deliberately NOT here - it predates Teams and holds real data; the
    meeting tests fence themselves by title instead.
    """
    from app.models import (
        TeamReaction, TeamAttachment, TeamMessage, TeamTyping,
        TeamPresence, TeamChannelMember, TeamChannel,
    )
    return [
        TeamReaction, TeamAttachment, TeamMessage, TeamTyping,
        TeamPresence, TeamChannelMember, TeamChannel,
    ]


def _clean():
    for model in _social_models():
        _db.session.query(model).delete()
    for model in _teams_models():
        _db.session.query(model).delete()
    _db.session.commit()


@pytest.fixture(scope="session")
def app():
    application = create_app()
    if registry.get("fake") is None:
        registry.register(FakeProvider())
    yield application


@pytest.fixture()
def session(app):
    with app.app_context():
        FakeProvider.mode = "ok"
        FakeProvider.comments = []
        _clean()
        yield _db.session
        _db.session.rollback()
        _clean()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def make_target(session):
    """Factory: build an approved, scheduled (due) target on the fake
    platform, returning (account, post, target)."""
    def _make(platform="fake", account_status="active", when_past=True,
              post_status="approved", post_type="image"):
        from app.models import (
            SocialAccount, SocialPost, SocialPostTarget, SocialMediaAsset,
        )
        from app.social.tokens.vault import get_vault

        acct = SocialAccount(
            platform=platform, external_id="EXT1", display_name="Fake Page",
            account_type="page", status=account_status,
            token_ciphertext=get_vault().encrypt("AT"), token_key_version=1,
        )
        session.add(acct)
        session.flush()

        post = SocialPost(title="t", base_caption="c", status=post_status,
                          approved_at=datetime.utcnow())
        session.add(post)
        session.flush()

        when = (datetime.utcnow() - timedelta(minutes=1)) if when_past \
            else (datetime.utcnow() + timedelta(hours=1))
        target = SocialPostTarget(
            social_post_id=post.id, social_account_id=acct.id,
            platform=platform, post_type=post_type, caption="hi",
            status="scheduled", scheduled_for=when,
        )
        session.add(target)
        session.flush()
        session.add(SocialMediaAsset(
            target_id=target.id, source="upload", object_key="x.jpg",
            role="main",
        ))
        session.commit()
        return acct, post, target

    return _make


# ======================================================================
# Users, roles and permissions
#
# The engine fixtures above deliberately never touch a business-domain
# table. These do - they have to create real users - so they are fenced
# two ways: every account they make carries the PYTEST_EMAIL_PREFIX, and
# cleanup only ever deletes rows matching it. DATABASE_URL is shared with
# the developer's own database, and a naive "DELETE FROM users" here would
# take their account and every task hanging off it.
# ======================================================================

#: Every account these tests create starts with this. Nothing without it
#: is ever deleted.
PYTEST_EMAIL_PREFIX = "pytest-role-"


@pytest.fixture(scope="session")
def permission_catalog(app):
    """The permission rows, seeded once. Uses seed_permissions() rather
    than seed_database() so the DEFAULT_ADMIN_* environment variables are
    not needed just to run the suite."""
    from app.seed import seed_permissions

    with app.app_context():
        seed_permissions()

    yield


def _delete_children_of(table, ids):
    """Delete every row in every table holding a foreign key to
    `table`.`id` with one of these ids."""
    from sqlalchemy import inspect, text

    if not ids:
        return

    inspector = inspect(_db.engine)
    id_list = ", ".join(str(int(i)) for i in ids)

    for other in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(other):
            if fk.get("referred_table") != table:
                continue
            for column in fk.get("constrained_columns", []):
                _db.session.execute(text(
                    f"DELETE FROM {other} WHERE {column} IN ({id_list})"
                ))


def _purge_test_rows():
    """Remove everything these fixtures create, in dependency order.

    Tasks first: a task holds a foreign key to its assignee, so a leftover
    from a test that failed part-way through would otherwise block every
    later run's user cleanup - and the cleanup is the only thing keeping
    this suite from touching the developer's own account.
    """
    from app.models import (
        Client, ClientDeliverable, ClientMonthlyTarget, Task, TaskComment,
        User, UserPermission,
    )

    # Meetings, fenced by title. The table predates Teams and holds real
    # rows, so unlike teams_* it is never truncated - only the ones a test
    # made are removed, the same way test tasks and clients are.
    from app.models import Meeting
    from app.models.meeting import meeting_participants
    meeting_ids = [
        row.id for row in _db.session.query(Meeting.id)
        .filter(Meeting.title.like(f"{PYTEST_EMAIL_PREFIX}%")).all()
    ]
    if meeting_ids:
        _db.session.execute(
            meeting_participants.delete().where(
                meeting_participants.c.meeting_id.in_(meeting_ids))
        )
        Meeting.query.filter(
            Meeting.id.in_(meeting_ids)
        ).delete(synchronize_session=False)

    task_ids = [
        row.id for row in _db.session.query(Task.id)
        .filter(Task.title.like(f"{PYTEST_EMAIL_PREFIX}%")).all()
    ]

    if task_ids:
        # Whatever hangs off a task - comments, the activity trail, files -
        # has to go first. Asking the database which tables reference
        # `tasks` beats keeping a hand-written list in step with the
        # schema: commenting alone writes to two of them.
        _delete_children_of("tasks", task_ids)

        Task.query.filter(
            Task.id.in_(task_ids)
        ).delete(synchronize_session=False)

    client_ids = [
        row.id for row in _db.session.query(Client.id)
        .filter(Client.client_name.like(f"{PYTEST_EMAIL_PREFIX}%")).all()
    ]

    if client_ids:
        target_ids = [
            row.id for row in _db.session.query(ClientMonthlyTarget.id)
            .filter(ClientMonthlyTarget.client_id.in_(client_ids)).all()
        ]
        if target_ids:
            ClientDeliverable.query.filter(
                ClientDeliverable.monthly_target_id.in_(target_ids)
            ).delete(synchronize_session=False)
            ClientMonthlyTarget.query.filter(
                ClientMonthlyTarget.id.in_(target_ids)
            ).delete(synchronize_session=False)

        # Brand assets hold a plain FK to clients with no cascade, so a
        # test that uploads one leaves the client undeletable here.
        from app.models import ClientAsset
        ClientAsset.query.filter(
            ClientAsset.client_id.in_(client_ids)
        ).delete(synchronize_session=False)

        Client.query.filter(
            Client.id.in_(client_ids)
        ).delete(synchronize_session=False)

    user_ids = [
        row.id for row in
        _db.session.query(User.id)
        .filter(User.email.like(f"{PYTEST_EMAIL_PREFIX}%")).all()
    ]

    if user_ids:
        UserPermission.query.filter(
            UserPermission.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        UserPermission.query.filter(
            UserPermission.granted_by_id.in_(user_ids)
        ).delete(synchronize_session=False)
        # Studio rows point at the user who made them, and those FKs block
        # the delete below. Detach rather than delete: the social tables
        # are wiped by their own fixture, and the audit trail is meant to
        # outlive the row it names (the app does the same in
        # _detach_post_history). Every nullable user FK on a social table
        # belongs here - a missed one surfaces as a teardown-only
        # ForeignKeyViolation, which is a confusing way to find out.
        from app.models import (
            ContentVersion, Meeting, Notification, SocialAccount,
            SocialAuditLog, SocialPost, SocialPostTarget, TeamChannel,
            TeamMessage,
        )
        for model, columns in (
            (SocialAuditLog, ("actor_id",)),
            (SocialPost, ("created_by_id", "approved_by_id")),
            (SocialPostTarget, ("story_link_done_by_id",)),
            (SocialAccount, ("connected_by_id",)),
            (ContentVersion, ("edited_by_id",)),
            (Notification, ("actor_id",)),
            # Teams: created_by_id on both of these is a plain FK with no
            # ON DELETE, so it blocks the user delete below. teams_messages
            # already has ON DELETE SET NULL, but it is detached here too
            # so the teardown does not depend on which layer runs first.
            (TeamChannel, ("created_by_id",)),
            (TeamMessage, ("user_id",)),
            (Meeting, ("created_by_id",)),
        ):
            for column in columns:
                model.query.filter(
                    getattr(model, column).in_(user_ids)
                ).update({column: None}, synchronize_session=False)

        # notifications.user_id is NOT NULL - a notification only exists
        # for its recipient, so it goes with them rather than detaching.
        Notification.query.filter(
            Notification.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        User.query.filter(
            User.id.in_(user_ids)
        ).delete(synchronize_session=False)

    _db.session.commit()


@pytest.fixture()
def make_user(app, permission_catalog):
    """Factory: make_user(role, permissions=[...]) -> User.

    Yields inside an app context so the returned objects stay attached to
    a live session for the duration of the test.
    """
    from werkzeug.security import generate_password_hash

    from app.models import Permission, User, UserPermission

    with app.app_context():
        _purge_test_rows()

        counter = {"n": 0}

        def _make(role, permissions=(), status="active", name=None):
            counter["n"] += 1
            n = counter["n"]

            user = User(
                name=name or f"Test {role} {n}",
                email=f"{PYTEST_EMAIL_PREFIX}{n}@example.invalid",
                phone="0000000000",
                password_hash=generate_password_hash("not-a-real-password"),
                role=role,
                status=status,
            )
            _db.session.add(user)
            _db.session.flush()

            for code in permissions:
                permission = Permission.query.filter_by(code=code).first()
                assert permission is not None, f"unseeded permission {code}"
                _db.session.add(UserPermission(
                    user_id=user.id, permission_id=permission.id,
                ))

            _db.session.commit()
            return user

        yield _make

        _purge_test_rows()


@pytest.fixture()
def login(client):
    """Sign a user in by writing the Flask-Login session directly, so the
    permission tests do not depend on the login form or on rate limits."""
    from flask import g

    def _login(user):
        with client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True

        # Flask-Login caches the resolved user on `g`, and `g` belongs to
        # the APP context - which a test holding its own app_context()
        # keeps open across several requests. Without this, switching user
        # mid-test silently keeps serving the first one.
        g.pop("_login_user", None)

        return client

    return _login


@pytest.fixture(autouse=True)
def _no_csrf(app):
    """Forms in tests post without a token.

    CSRF is exercised by csrf.js and the real browser flow; making every
    test thread a token through would test Flask-WTF rather than this
    application's authorisation rules.
    """
    previous = app.config.get("WTF_CSRF_ENABLED", True)
    app.config["WTF_CSRF_ENABLED"] = False
    yield
    app.config["WTF_CSRF_ENABLED"] = previous


@pytest.fixture()
def make_task(app, make_user):
    """Factory: make_task(assignee) -> Task, on a throwaway client.

    tasks.client_id is NOT NULL, so a task needs a client even when the
    test only cares about who may read it. Both are prefixed and removed
    afterwards, like the users.
    """
    from app.models import (
        Client, ClientDeliverable, ClientMonthlyTarget, Task, TaskComment,
    )

    with app.app_context():

        def _make(assignee, title="pytest-role-task"):
            # A task needs a client AND a deliverable - both columns are
            # NOT NULL - so the whole little chain gets built and torn
            # down again.
            client = Client(
                client_name=f"{PYTEST_EMAIL_PREFIX}client",
                status="active",
            )
            _db.session.add(client)
            _db.session.flush()

            target = ClientMonthlyTarget(
                client_id=client.id, month=1, year=2026,
            )
            _db.session.add(target)
            _db.session.flush()

            deliverable = ClientDeliverable(
                monthly_target_id=target.id,
                service_name="Testing",
                deliverable_name="pytest deliverable",
            )
            _db.session.add(deliverable)
            _db.session.flush()

            task = Task(
                title=title,
                status="Assigned",
                client_id=client.id,
                deliverable_id=deliverable.id,
                assigned_to_id=assignee.id,
                created_by_id=assignee.id,
                deadline=datetime.utcnow() + timedelta(days=2),
            )
            _db.session.add(task)
            _db.session.commit()

            return task

        yield _make

        # One cleanup path, shared with the user fixture: it walks the
        # foreign keys, so a comment or an activity row written by the
        # test itself cannot wedge the delete.
        _purge_test_rows()
