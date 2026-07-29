"""Handing a task to someone else, and getting it back.

The bug these were written for: user1 self-assigns a task, transfers it to
user2, user2 cannot rename it so transfers it back — and user1 cannot open
the notification, because a transfer recipient is not the assignee and so
cannot see the task the Accept button lives on. The task was stuck.

Note for anyone adding a rename test: every title must keep the
`pytest-role-` prefix. The fixture cleanup in conftest finds test tasks by
that prefix, and a task renamed out of it is orphaned in the developer's
own database — where it then blocks user cleanup on every later run,
because tasks hold a foreign key to their assignee.
"""

import pytest

from app.extensions import db
from app.models import Task, TaskActivity, TaskTransferRequest


def _reassign(task, user):
    """Move a task to someone else, the way a manager or an accepted
    transfer would, so the "no longer mine" state is real."""
    task.assigned_to_id = user.id
    db.session.commit()


def _request_transfer(client, task_id, to_user):
    return client.post(
        f"/tasks/{task_id}/transfer/request",
        data={"to_user_id": str(to_user.id), "message": "please take this"},
        follow_redirects=True,
    )


# ----------------------------------------------------------------------
# The reported bug
# ----------------------------------------------------------------------

def test_the_person_asked_to_take_a_task_can_open_it(
        app, client, make_user, make_task, login):
    """The headline case. Before the fix this was a redirect, which is what
    made the task drawer flash open and close again."""
    with app.app_context():
        u1 = make_user("video_editor")
        u2 = make_user("graphic_designer")

        task = make_task(u1)          # self-assigned: creator == assignee
        _reassign(task, u2)           # u1 hands it over

        login(u2)
        _request_transfer(client, task.id, u1)   # u2 asks for it back

        login(u1)
        response = client.get(f"/tasks/{task.id}", follow_redirects=False)

        assert response.status_code == 200


def test_the_requester_keeps_sight_of_the_task_while_it_is_pending(
        app, client, make_user, make_task, login):
    with app.app_context():
        u1 = make_user("video_editor")
        u2 = make_user("content_writer")

        task = make_task(u1)
        _reassign(task, u2)

        login(u2)
        _request_transfer(client, task.id, u1)

        # u2 is still the assignee here, so this asserts the from-side
        # clause rather than the assignee one: swap them round.
        _reassign(task, u1)
        login(u2)

        assert client.get(f"/tasks/{task.id}").status_code == 200


def test_the_task_shows_up_in_the_recipients_list(
        app, client, make_user, make_task, login):
    with app.app_context():
        u1 = make_user("video_editor")
        u2 = make_user("graphic_designer")

        task = make_task(u1)
        _reassign(task, u2)

        login(u2)
        _request_transfer(client, task.id, u1)

        login(u1)
        body = client.get("/tasks/").get_data(as_text=True)

        assert task.title in body


def test_the_transfer_can_be_accepted_end_to_end(
        app, client, make_user, make_task, login):
    with app.app_context():
        u1 = make_user("video_editor")
        u2 = make_user("graphic_designer")

        task = make_task(u1)
        _reassign(task, u2)

        login(u2)
        _request_transfer(client, task.id, u1)
        transfer = TaskTransferRequest.pending_for(task.id)
        assert transfer is not None

        login(u1)
        client.post(f"/tasks/transfer/{transfer.id}/accept",
                    follow_redirects=True)

        db.session.refresh(task)
        db.session.refresh(transfer)

        assert task.assigned_to_id == u1.id
        assert transfer.status == TaskTransferRequest.ACCEPTED


def test_the_view_ends_when_the_request_is_answered(
        app, client, make_user, make_task, login):
    """Pending only. The access is a rule about a live request, not a
    share that somebody has to remember to take away."""
    with app.app_context():
        u1 = make_user("video_editor")
        u2 = make_user("graphic_designer")

        task = make_task(u1)
        _reassign(task, u2)

        login(u2)
        _request_transfer(client, task.id, u1)
        transfer = TaskTransferRequest.pending_for(task.id)

        login(u1)
        assert client.get(f"/tasks/{task.id}").status_code == 200

        client.post(f"/tasks/transfer/{transfer.id}/decline",
                    data={"response_message": "not mine"},
                    follow_redirects=True)

        # u1 created it but no longer holds it: back to no access.
        response = client.get(f"/tasks/{task.id}", follow_redirects=False)
        assert response.status_code == 302


def test_an_unrelated_user_is_still_refused_while_a_transfer_is_pending(
        app, client, make_user, make_task, login):
    """Catches an EXISTS written without correlating on task_id."""
    with app.app_context():
        u1 = make_user("video_editor")
        u2 = make_user("graphic_designer")
        outsider = make_user("content_writer")

        task = make_task(u1)
        _reassign(task, u2)

        login(u2)
        _request_transfer(client, task.id, u1)

        login(outsider)
        response = client.get(f"/tasks/{task.id}", follow_redirects=False)

        assert response.status_code == 302


# ----------------------------------------------------------------------
# The denial itself
# ----------------------------------------------------------------------

def test_a_refused_task_in_the_drawer_does_not_redirect(
        app, client, make_user, make_task, login):
    """The drawer watches the iframe's path: a redirect reads as "the panel
    left the task", so it closes itself and hard-reloads the page behind.
    Staying on the same url is the whole fix."""
    with app.app_context():
        owner = make_user("video_editor")
        outsider = make_user("content_writer")
        task = make_task(owner)

        login(outsider)
        response = client.get(f"/tasks/{task.id}?panel=1",
                              follow_redirects=False)

        assert response.status_code == 403
        assert "Location" not in response.headers
        assert "can’t open this task" in response.get_data(as_text=True)


def test_a_refused_task_explains_itself_on_a_full_page(
        app, client, make_user, make_task, login):
    with app.app_context():
        owner = make_user("video_editor")
        outsider = make_user("content_writer")
        task = make_task(owner)

        login(outsider)
        body = client.get(f"/tasks/{task.id}",
                          follow_redirects=True).get_data(as_text=True)

        assert "do not have access" in body


# ----------------------------------------------------------------------
# Renaming
# ----------------------------------------------------------------------

def _rename(client, task_id, value):
    return client.post(f"/tasks/{task_id}/quick-update",
                       json={"field": "title", "value": value})


def test_an_assignee_can_rename_a_task_they_did_not_create(
        app, client, make_user, make_task, login):
    """The reason the whole round-trip happened: the new owner of a
    transferred task could not correct its name."""
    with app.app_context():
        u1 = make_user("video_editor")
        u2 = make_user("graphic_designer")

        task = make_task(u1)
        _reassign(task, u2)

        login(u2)
        response = _rename(client, task.id, "pytest-role-renamed by the assignee")

        assert response.status_code == 200
        assert response.get_json()["success"] is True

        db.session.refresh(task)
        assert task.title == "pytest-role-renamed by the assignee"


def test_a_rename_is_recorded_on_the_timeline(
        app, client, make_user, make_task, login):
    with app.app_context():
        u1 = make_user("video_editor")
        u2 = make_user("graphic_designer")

        task = make_task(u1)
        _reassign(task, u2)

        login(u2)
        _rename(client, task.id, "pytest-role-a new name")

        entries = TaskActivity.query.filter_by(task_id=task.id).all()
        assert any("Task Name" in (e.message or "") for e in entries)


def test_an_outsider_cannot_rename(
        app, client, make_user, make_task, login):
    with app.app_context():
        owner = make_user("video_editor")
        outsider = make_user("content_writer")
        task = make_task(owner)
        original = task.title

        login(outsider)
        response = _rename(client, task.id, "pytest-role-not yours")

        assert response.status_code == 403
        db.session.refresh(task)
        assert task.title == original


@pytest.mark.parametrize("value", ["", "   ", "pytest-role-" + "x" * 250])
def test_rename_rejects_empty_and_overlong_names(
        app, client, make_user, make_task, login, value):
    with app.app_context():
        owner = make_user("video_editor")
        task = make_task(owner)
        original = task.title

        login(owner)
        response = _rename(client, task.id, value)

        assert response.status_code == 400
        db.session.refresh(task)
        assert task.title == original


def test_renaming_does_not_widen_the_other_quick_edits(
        app, client, make_user, make_task, login):
    """The regression guard for the per-field gate. Opening `title` to
    everyone with access must not open priority with it."""
    with app.app_context():
        u1 = make_user("video_editor")
        u2 = make_user("graphic_designer")

        task = make_task(u1)
        _reassign(task, u2)
        original_priority = task.priority

        login(u2)
        response = client.post(f"/tasks/{task.id}/quick-update",
                               json={"field": "priority", "value": "Urgent"})

        assert response.status_code == 403
        db.session.refresh(task)
        assert task.priority == original_priority


def test_a_closed_task_cannot_be_renamed(
        app, client, make_user, make_task, login):
    with app.app_context():
        owner = make_user("video_editor")
        task = make_task(owner)
        task.status = "Published"
        db.session.commit()
        original = task.title

        login(owner)
        response = _rename(client, task.id, "pytest-role-too late")

        assert response.status_code == 400
        db.session.refresh(task)
        assert task.title == original
