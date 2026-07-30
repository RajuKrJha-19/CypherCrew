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
        user = make_user("video_editor", permissions=["approve_tasks"])

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
        user = make_user("content_writer", permissions=["view_reports"])
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
        user = make_user("social_media_manager")

        perms.apply_role_defaults(user, commit=True)
        first = perms.granted_codes(user)

        perms.apply_role_defaults(user, commit=True)

        assert perms.granted_codes(user) == first
        assert len(user.permissions) == len(first)


def test_apply_role_defaults_replaces_rather_than_merges(app, make_user):
    with app.app_context():
        user = make_user("video_editor", permissions=["manage_tasks"])

        perms.apply_role_defaults(user, commit=True)

        # A reset that cannot take anything away is not a reset.
        assert perms.granted_codes(user) == set()


def test_setting_permissions_records_who_granted_them(app, make_user):
    with app.app_context():
        actor = make_user("admin")
        user = make_user("graphic_designer")

        perms.set_permissions(
            user, ["view_all_tasks"], granted_by=actor, commit=True,
        )

        row = user.permissions[0]
        assert row.granted_by_id == actor.id
        assert row.granted_at is not None


def test_setting_permissions_ignores_unknown_codes(app, make_user):
    with app.app_context():
        user = make_user("content_writer")

        perms.set_permissions(
            user, ["view_reports", "make_me_an_owner"], commit=True,
        )

        assert perms.granted_codes(user) == {"view_reports"}


# ----------------------------------------------------------------------
# The capability helpers
# ----------------------------------------------------------------------

def test_senior_craft_reviews_but_does_not_publish(app, make_user):
    with app.app_context():
        senior = make_user("video_editor_senior")
        perms.apply_role_defaults(senior, commit=True)

        assert perms.has_permission(senior, "approve_tasks")
        assert perms.can_view_all_tasks(senior)
        assert perms.can_review(senior)
        assert not perms.can_publish(senior)


def test_manager_publishes(app, make_user):
    with app.app_context():
        manager = make_user("social_media_manager")
        perms.apply_role_defaults(manager, commit=True)

        assert perms.can_publish(manager)
        assert perms.can_use_social(manager)
        assert perms.can_connect_social_accounts(manager)


def test_junior_holds_nothing(app, make_user):
    with app.app_context():
        junior = make_user("social_media_executive")
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
        ops = make_user("software_developer",
                        permissions=["manage_social_engine"])

        # Previously role-only, so it could not be handed to whoever
        # actually babysits the queue.
        assert perms.can_manage_social_engine(ops)


# ----------------------------------------------------------------------
# Reaching the permanent shell
# ----------------------------------------------------------------------
#
# The sidebar and topbar are data-turbo-permanent: rendered once per real
# page load and reused for every Turbo navigation afterwards. They are also
# built almost entirely from the capability helpers above, so a grant made
# on the permissions screen was invisible to the person who received it -
# their nav kept whatever it was first built with. The save had worked;
# nothing said so, which is indistinguishable from a save that had not.

def test_access_fingerprint_moves_when_a_permission_is_granted(app, make_user):
    """If the digest does not change, Turbo has no reason to reload and the
    stale sidebar survives the navigation."""
    from app.models import User

    with app.app_context():
        user = make_user("video_editor", permissions=["approve_tasks"])
        before = perms.access_fingerprint(user)

        perms.set_permissions(user, {"approve_tasks", "view_reports"},
                              commit=True)
        db.session.expire_all()

        assert perms.access_fingerprint(User.query.get(user.id)) != before


def test_access_fingerprint_separates_two_people(app, make_user):
    with app.app_context():
        one = make_user("video_editor", permissions=["approve_tasks"])
        two = make_user("video_editor",
                        permissions=["approve_tasks", "view_reports"])

        assert perms.access_fingerprint(one) != perms.access_fingerprint(two)


def test_access_fingerprint_is_stable_for_an_unchanged_user(app, make_user):
    """A digest that moved on its own would full-reload every navigation,
    throwing away the whole point of the permanent shell."""
    with app.app_context():
        user = make_user("video_editor", permissions=["approve_tasks"])
        assert perms.access_fingerprint(user) == perms.access_fingerprint(user)


def test_access_fingerprint_handles_no_user(app):
    with app.app_context():
        assert perms.access_fingerprint(None) == "anon"


def test_the_shell_carries_the_tracked_digest(app, client, login, make_user):
    with app.app_context():
        login(make_user("super_admin"))
        body = client.get("/permissions/").get_data(as_text=True)

        assert 'name="cc-access"' in body
        assert 'data-turbo-track="reload"' in body


# ----------------------------------------------------------------------
# The permissions screen: a refusal must not read as a success
# ----------------------------------------------------------------------

def test_a_delegate_is_told_that_a_meta_permission_was_refused(
        app, client, login, make_user):
    """Only the owner may hand out manage_users / manage_permissions. The
    route drops the tick and saves the rest, which is right - but it used to
    do so under a "Permissions updated" flash and no other word, so the
    permission looked like it had failed to save."""
    from app.models import User

    with app.app_context():
        delegate = make_user("admin", permissions=["manage_permissions"])
        target = make_user("video_editor", permissions=["approve_tasks"])
        target_id = target.id
        login(delegate)

        response = client.post(
            f"/permissions/user/{target_id}",
            data={"permissions": ["approve_tasks", "view_reports",
                                  "manage_users"]},
            follow_redirects=True,
        )

        db.session.expire_all()
        held = perms.granted_codes(User.query.get(target_id))

        assert "manage_users" not in held, "the guard itself must not weaken"
        assert "view_reports" in held, "the legitimate half must still land"
        assert "only the owner can grant or revoke" in \
            response.get_data(as_text=True)


def test_the_owner_may_still_grant_a_meta_permission(
        app, client, login, make_user):
    from app.models import User

    with app.app_context():
        owner = make_user("super_admin")
        target = make_user("video_editor", permissions=["approve_tasks"])
        target_id = target.id
        login(owner)

        response = client.post(
            f"/permissions/user/{target_id}",
            data={"permissions": ["approve_tasks", "manage_users"]},
            follow_redirects=True,
        )

        db.session.expire_all()

        assert "manage_users" in perms.granted_codes(User.query.get(target_id))
        assert "only the owner can grant or revoke" not in \
            response.get_data(as_text=True)
