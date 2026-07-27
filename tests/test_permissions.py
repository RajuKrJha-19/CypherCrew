"""has_permission, the role defaults, and the guards that hang off them.

These are the first tests the user/permission code has ever had. Every
account they create carries the pytest email prefix and is deleted
afterwards - see the fixtures in conftest.py, and note that DATABASE_URL
is shared with the developer's own database.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.utils import permissions as perms
from app.utils import roles


# ----------------------------------------------------------------------
# has_permission
# ----------------------------------------------------------------------

def test_owner_holds_everything_without_any_rows(app, make_user):
    with app.app_context():
        owner = make_user("super_admin")

        assert owner.permissions == []
        assert perms.has_permission(owner, "manage_tasks")
        # Including a code that does not exist: the owner bypass is a rule
        # about the account, not a lookup.
        assert perms.has_permission(owner, "not_a_real_permission")


def test_granted_and_missing_permissions(app, make_user):
    with app.app_context():
        user = make_user("junior_video_editor", permissions=["approve_tasks"])

        assert perms.has_permission(user, "approve_tasks")
        assert not perms.has_permission(user, "manage_tasks")


def test_no_user_is_not_an_error(app):
    with app.app_context():
        # This used to raise AttributeError on user.role. It runs on every
        # request; it must never be the thing that takes a page down.
        assert perms.has_permission(None, "manage_tasks") is False


def test_duplicate_grants_are_refused_by_the_database(app, make_user):
    from app.models import Permission, UserPermission

    with app.app_context():
        user = make_user("junior_content_writer", permissions=["view_reports"])
        permission = Permission.query.filter_by(code="view_reports").first()

        db.session.add(UserPermission(
            user_id=user.id, permission_id=permission.id,
        ))

        with pytest.raises(IntegrityError):
            db.session.commit()

        db.session.rollback()


# ----------------------------------------------------------------------
# Role defaults
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "role_value",
    [r.value for r in roles.ROLE_LIST if not roles.is_owner(r.value)],
)
def test_apply_role_defaults_grants_exactly_the_defaults(
        app, make_user, role_value):
    with app.app_context():
        user = make_user(role_value)

        perms.apply_role_defaults(user, commit=True)

        assert perms.granted_codes(user) == roles.defaults_for(role_value)


def test_apply_role_defaults_is_idempotent(app, make_user):
    with app.app_context():
        user = make_user("senior_social_media_manager")

        perms.apply_role_defaults(user, commit=True)
        first = perms.granted_codes(user)

        perms.apply_role_defaults(user, commit=True)

        assert perms.granted_codes(user) == first
        assert len(user.permissions) == len(first)


def test_apply_role_defaults_replaces_rather_than_merges(app, make_user):
    with app.app_context():
        user = make_user("junior_video_editor", permissions=["manage_tasks"])

        perms.apply_role_defaults(user, commit=True)

        # A reset that cannot take anything away is not a reset.
        assert perms.granted_codes(user) == set()


def test_setting_permissions_records_who_granted_them(app, make_user):
    with app.app_context():
        actor = make_user("admin")
        user = make_user("junior_graphic_designer")

        perms.set_permissions(
            user, ["view_all_tasks"], granted_by=actor, commit=True,
        )

        row = user.permissions[0]
        assert row.granted_by_id == actor.id
        assert row.granted_at is not None


def test_setting_permissions_ignores_unknown_codes(app, make_user):
    with app.app_context():
        user = make_user("junior_content_writer")

        perms.set_permissions(
            user, ["view_reports", "make_me_an_owner"], commit=True,
        )

        assert perms.granted_codes(user) == {"view_reports"}


# ----------------------------------------------------------------------
# The capability helpers
# ----------------------------------------------------------------------

def test_senior_craft_reviews_but_does_not_publish(app, make_user):
    with app.app_context():
        senior = make_user("senior_video_editor")
        perms.apply_role_defaults(senior, commit=True)

        assert perms.has_permission(senior, "approve_tasks")
        assert perms.can_view_all_tasks(senior)
        assert perms.can_review(senior)
        assert not perms.can_publish(senior)


def test_manager_publishes(app, make_user):
    with app.app_context():
        manager = make_user("senior_social_media_manager")
        perms.apply_role_defaults(manager, commit=True)

        assert perms.can_publish(manager)
        assert perms.can_use_social(manager)
        assert perms.can_connect_social_accounts(manager)


def test_junior_holds_nothing(app, make_user):
    with app.app_context():
        junior = make_user("junior_social_media_executive")
        perms.apply_role_defaults(junior, commit=True)

        # Studio only - the one thing this role exists to do.
        assert perms.can_use_social(junior)
        assert not perms.can_view_all_tasks(junior)
        assert not perms.can_assign_tasks(junior)
        assert not perms.can_review(junior)
        assert not perms.can_publish(junior)
        assert not perms.can_view_team_performance(junior)
        assert not perms.can_manage_users(junior)


def test_manage_tasks_still_carries_its_old_reach(app, make_user):
    """The transitional clauses that keep today's team leads whole.

    Nobody holds view_all_tasks or the people-ops codes yet, so the split
    must not take anything away from someone who has manage_tasks now.
    """
    with app.app_context():
        lead = make_user("employee", permissions=["manage_tasks"])

        assert perms.can_view_all_tasks(lead)
        assert perms.can_assign_tasks(lead)
        assert perms.can_manage_leaves(lead)
        assert perms.can_manage_holidays(lead)
        assert perms.can_manage_meetings(lead)


def test_engine_operation_can_now_be_delegated(app, make_user):
    with app.app_context():
        ops = make_user("junior_software_developer",
                        permissions=["manage_social_engine"])

        # Previously role-only, so it could not be handed to whoever
        # actually babysits the queue.
        assert perms.can_manage_social_engine(ops)
