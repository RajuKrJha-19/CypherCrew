"""Route-level access: where each role lands, and what each role is
refused.

The permission tests next door check the rules; these check that the rules
are actually wired to the routes. Every one of these corresponds to a hole
or a breakage found while rebuilding the role architecture.
"""

import pytest

from app.utils import permissions as perms
from app.utils import roles


@pytest.mark.parametrize(
    "role_value", roles.ALL_ROLE_VALUES,
)
def test_every_role_lands_on_a_dashboard_and_is_never_logged_out(
        app, client, make_user, login, role_value):
    """The blocker this whole rebuild had to clear first.

    dashboard.index listed exactly three roles and fell through to
    auth.logout, so the first person given any other role would have been
    signed out every time they signed in - a login loop with no error to
    explain it.
    """
    with app.app_context():
        user = make_user(role_value)
        login(user)

        response = client.get("/", follow_redirects=False)

        assert response.status_code == 302
        location = response.headers["Location"]
        assert "logout" not in location
        assert location.rstrip("/").endswith(
            {
                "dashboard.super_admin": "super-admin",
                "dashboard.admin": "admin",
                "dashboard.employee": "employee",
            }[roles.dashboard_endpoint(role_value)]
        )


def test_a_role_left_over_from_an_older_deploy_still_signs_in(
        app, client, make_user, login):
    with app.app_context():
        user = make_user("employee")
        user.role = "some_retired_role"
        from app.extensions import db
        db.session.commit()

        login(user)
        response = client.get("/", follow_redirects=False)

        assert response.status_code == 302
        assert "logout" not in response.headers["Location"]


@pytest.mark.parametrize("path", ["/super-admin", "/admin"])
def test_management_dashboards_are_guarded(
        app, client, make_user, login, path):
    """These had no guard at all - only the post-login redirect kept
    people away, so typing the URL was enough to read company-wide
    delivery figures, workload and per-person performance."""
    with app.app_context():
        junior = make_user("video_editor")
        perms.apply_role_defaults(junior, commit=True)
        login(junior)

        response = client.get(path, follow_redirects=False)

        assert response.status_code == 302
        assert "/super-admin" not in response.headers["Location"]


@pytest.mark.parametrize("path", ["/super-admin", "/admin"])
def test_management_dashboards_open_for_an_admin(
        app, client, make_user, login, path):
    with app.app_context():
        admin = make_user("admin")
        login(admin)

        assert client.get(path).status_code == 200


def test_api_overview_is_refused_for_a_junior(
        app, client, make_user, login):
    with app.app_context():
        junior = make_user("content_writer")
        login(junior)

        assert client.get("/api/overview").status_code == 403


def test_review_queue_needs_a_review_permission(
        app, client, make_user, login):
    with app.app_context():
        junior = make_user("graphic_designer")
        login(junior)
        assert client.get("/my-tasks", follow_redirects=False).status_code == 302

        senior = make_user("graphic_designer_senior")
        perms.apply_role_defaults(senior, commit=True)
        login(senior)
        assert client.get("/my-tasks").status_code == 200


def test_user_administration_needs_manage_users(
        app, client, make_user, login):
    with app.app_context():
        junior = make_user("software_developer")
        login(junior)
        assert client.get("/users/", follow_redirects=False).status_code == 302

        # The permission used to gate only the sidebar link, so granting it
        # produced a link that bounced you back to the dashboard.
        granted = make_user("software_developer_senior",
                            permissions=["manage_users"])
        login(granted)
        assert client.get("/users/").status_code == 200


def test_permissions_screen_needs_manage_permissions(
        app, client, make_user, login):
    with app.app_context():
        senior = make_user("content_writer_senior")
        login(senior)
        assert client.get("/permissions/",
                          follow_redirects=False).status_code == 302

        granted = make_user("content_writer_senior",
                            permissions=["manage_permissions"])
        login(granted)
        assert client.get("/permissions/").status_code == 200


def test_permission_holder_cannot_edit_their_own_access(
        app, client, make_user, login):
    """Delegating manage_permissions must not be the same as handing over
    the company."""
    with app.app_context():
        granted = make_user("video_editor_senior",
                            permissions=["manage_permissions"])
        login(granted)

        response = client.get(f"/permissions/user/{granted.id}",
                              follow_redirects=False)

        assert response.status_code == 302


def test_permission_holder_cannot_edit_an_administrator(
        app, client, make_user, login):
    with app.app_context():
        admin = make_user("admin")
        granted = make_user("video_editor_senior",
                            permissions=["manage_permissions"])
        login(granted)

        response = client.get(f"/permissions/user/{admin.id}",
                              follow_redirects=False)

        assert response.status_code == 302


def test_permission_holder_cannot_hand_out_the_meta_permissions(
        app, client, make_user, login):
    with app.app_context():
        granted = make_user("video_editor_senior",
                            permissions=["manage_permissions"])
        target = make_user("video_editor")
        login(granted)

        client.post(
            f"/permissions/user/{target.id}",
            data={"permissions": ["view_all_tasks", "manage_permissions",
                                  "manage_users"]},
            follow_redirects=True,
        )

        held = perms.granted_codes(target)
        assert "view_all_tasks" in held
        assert "manage_permissions" not in held
        assert "manage_users" not in held


def test_owner_can_hand_out_the_meta_permissions(
        app, client, make_user, login):
    with app.app_context():
        owner = make_user("super_admin")
        target = make_user("social_media_manager")
        login(owner)

        client.post(
            f"/permissions/user/{target.id}",
            data={"permissions": ["manage_users"]},
            follow_redirects=True,
        )

        assert "manage_users" in perms.granted_codes(target)


def test_apply_role_defaults_route_is_post_only(
        app, client, make_user, login):
    with app.app_context():
        owner = make_user("super_admin")
        target = make_user("content_writer_senior")
        login(owner)

        assert client.get(
            f"/permissions/user/{target.id}/apply-role-defaults"
        ).status_code == 405

        client.post(
            f"/permissions/user/{target.id}/apply-role-defaults",
            follow_redirects=True,
        )

        assert perms.granted_codes(target) == roles.defaults_for(
            "content_writer_senior")


# ----------------------------------------------------------------------
# User administration
# ----------------------------------------------------------------------

def test_admin_cannot_create_an_administrator(app, client, make_user, login):
    from app.models import User

    with app.app_context():
        admin = make_user("admin")
        login(admin)

        client.post("/users/add", data={
            "name": "Sneaky",
            "email": "pytest-role-escalation@example.invalid",
            "password": "hunter2hunter2",
            "role": "admin",
            "status": "active",
        }, follow_redirects=True)

        assert User.query.filter_by(
            email="pytest-role-escalation@example.invalid").first() is None


def test_a_role_off_the_catalog_is_refused(app, client, make_user, login):
    from app.models import User

    with app.app_context():
        owner = make_user("super_admin")
        login(owner)

        client.post("/users/add", data={
            "name": "Nonsense",
            "email": "pytest-role-nonsense@example.invalid",
            "password": "hunter2hunter2",
            "role": "director_of_vibes",
            "status": "active",
        }, follow_redirects=True)

        # users.py wrote whatever the form posted, unvalidated, straight
        # into the column.
        assert User.query.filter_by(
            email="pytest-role-nonsense@example.invalid").first() is None


def test_a_new_account_starts_with_its_role_defaults(
        app, client, make_user, login):
    from app.models import User

    with app.app_context():
        owner = make_user("super_admin")
        login(owner)

        client.post("/users/add", data={
            "name": "New Senior",
            "email": "pytest-role-new@example.invalid",
            "password": "hunter2hunter2",
            "role": "graphic_designer_senior",
            "status": "active",
        }, follow_redirects=True)

        created = User.query.filter_by(
            email="pytest-role-new@example.invalid").first()

        assert created is not None
        assert perms.granted_codes(created) == roles.defaults_for(
            "graphic_designer_senior")


def test_nobody_changes_their_own_role(app, client, make_user, login):
    with app.app_context():
        owner = make_user("super_admin")
        login(owner)

        client.post(f"/users/edit/{owner.id}", data={
            "name": owner.name,
            "role": "video_editor",
            "status": "active",
        }, follow_redirects=True)

        assert owner.role == "super_admin"


# ----------------------------------------------------------------------
# The access holes closed alongside the rebuild
# ----------------------------------------------------------------------

def test_calendar_is_scoped_to_visible_tasks(
        app, client, make_user, make_task, login):
    """The calendar ran a bare Task.query, so anybody signed in could read
    the title, client, assignee and deadline of every task in the
    company."""
    with app.app_context():
        outsider = make_user("content_writer")
        other = make_user("video_editor")
        make_task(other, title="pytest-role-secret-task")

        login(outsider)
        body = client.get("/calendar").get_data(as_text=True)
        assert "pytest-role-secret-task" not in body

        login(other)
        body = client.get("/calendar").get_data(as_text=True)
        assert "pytest-role-secret-task" in body


def test_a_senior_sees_the_whole_calendar(
        app, client, make_user, make_task, login):
    from app.utils import permissions as perms

    with app.app_context():
        other = make_user("video_editor")
        make_task(other, title="pytest-role-visible-task")

        senior = make_user("video_editor_senior")
        perms.apply_role_defaults(senior, commit=True)

        login(senior)
        body = client.get("/calendar").get_data(as_text=True)

        assert "pytest-role-visible-task" in body


def test_commenting_needs_access_to_the_task(
        app, client, make_user, make_task, login):
    """task_detail was scoped but the comment POST behind it was not, so
    any user could comment on - and @-mention from - a task they could not
    open."""
    from app.models import TaskComment

    with app.app_context():
        outsider = make_user("content_writer")
        owner = make_user("video_editor")
        task = make_task(owner)

        login(outsider)
        client.post(f"/tasks/{task.id}/comment",
                    data={"message": "I should not be here"},
                    follow_redirects=True)

        assert TaskComment.query.filter_by(task_id=task.id).count() == 0

        login(owner)
        client.post(f"/tasks/{task.id}/comment",
                    data={"message": "my own task"},
                    follow_redirects=True)

        assert TaskComment.query.filter_by(task_id=task.id).count() == 1
