"""Moving a task between statuses by hand.

Three surfaces do it — dragging a card, the Status dropdown in the list,
and the stepper on the task page — and all three post to the same
endpoint, so these test the endpoint and the rules behind it.

Two rules are new here: Scheduled and Published belong to the publish
flow and cannot be set or left by hand, and leaving a review takes a
written reason.
"""

import pytest

from app.extensions import db
from app.models import TaskActivity
from app.utils import task_status


def _move(client, task, to_status, reason=None):
    payload = {"task_id": task.id, "status": to_status}
    if reason is not None:
        payload["reason"] = reason
    return client.post("/tasks/kanban/update-status", json=payload)


def _set_status(task, status):
    task.status = status
    db.session.commit()


# ----------------------------------------------------------------------
# The rules themselves
# ----------------------------------------------------------------------

def test_the_publish_flow_owns_scheduled_and_published():
    assert task_status.is_drag_locked(task_status.SCHEDULED)
    assert task_status.is_drag_locked(task_status.PUBLISHED)
    assert not task_status.is_drag_locked(task_status.CORE_REVIEW)


def test_hand_moves_never_offers_a_locked_or_reason_only_status():
    for status in task_status.ALL_STATUSES:
        for target in task_status.hand_moves(status, can_manage=True):
            assert target not in task_status.DRAG_LOCKED_STATUSES
            assert target not in task_status.REASON_REQUIRED_STATUSES


def test_nothing_can_be_hand_moved_out_of_a_locked_status():
    assert task_status.hand_moves(task_status.SCHEDULED, can_manage=True) == []
    assert task_status.hand_moves(task_status.PUBLISHED, can_manage=True) == []


def test_reviews_are_the_statuses_that_need_a_reason_to_leave():
    assert task_status.needs_reason_to_leave(task_status.CORE_REVIEW)
    assert task_status.needs_reason_to_leave(task_status.CLIENT_REVIEW)
    assert not task_status.needs_reason_to_leave(task_status.IN_PROGRESS)


# ----------------------------------------------------------------------
# Scheduled / Published are not hand-set
# ----------------------------------------------------------------------

@pytest.mark.parametrize("target", ["Scheduled", "Published"])
def test_a_task_cannot_be_dragged_into_the_publish_statuses(
        app, client, make_user, make_task, login, target):
    with app.app_context():
        manager = make_user("admin", permissions=["manage_tasks"])
        task = make_task(manager)
        _set_status(task, "Client Review")

        login(manager)
        response = _move(client, task, target)

        assert response.status_code == 400
        assert response.get_json()["success"] is False

        db.session.refresh(task)
        assert task.status == "Client Review"


@pytest.mark.parametrize("current", ["Scheduled", "Published"])
def test_a_task_cannot_be_dragged_out_of_the_publish_statuses(
        app, client, make_user, make_task, login, current):
    with app.app_context():
        manager = make_user("admin", permissions=["manage_tasks"])
        task = make_task(manager)
        _set_status(task, current)

        login(manager)
        response = _move(client, task, "In Progress", reason="changed my mind")

        assert response.status_code == 400
        db.session.refresh(task)
        assert task.status == current


# ----------------------------------------------------------------------
# Leaving a review takes a reason
# ----------------------------------------------------------------------

@pytest.mark.parametrize("review", ["Core Review", "Client Review"])
def test_leaving_a_review_without_a_reason_is_refused(
        app, client, make_user, make_task, login, review):
    with app.app_context():
        manager = make_user("admin", permissions=["manage_tasks"])
        task = make_task(manager)
        _set_status(task, review)

        login(manager)
        response = _move(client, task, "In Progress")
        body = response.get_json()

        assert response.status_code == 400
        assert body["success"] is False
        # The flag is what tells a caller that skipped the dialog to ask.
        assert body["needs_reason"] is True
        assert body["from_status"] == review

        db.session.refresh(task)
        assert task.status == review


def test_a_token_reason_is_not_enough(
        app, client, make_user, make_task, login):
    with app.app_context():
        manager = make_user("admin", permissions=["manage_tasks"])
        task = make_task(manager)
        _set_status(task, "Core Review")

        login(manager)
        response = _move(client, task, "In Progress", reason="ok")

        assert response.status_code == 400
        db.session.refresh(task)
        assert task.status == "Core Review"


def test_leaving_a_review_with_a_reason_works_and_is_recorded(
        app, client, make_user, make_task, login):
    with app.app_context():
        manager = make_user("admin", permissions=["manage_tasks"])
        task = make_task(manager)
        _set_status(task, "Core Review")

        login(manager)
        response = _move(client, task, "In Progress",
                         reason="thumbnail needs redoing")

        assert response.status_code == 200
        assert response.get_json()["success"] is True

        db.session.refresh(task)
        assert task.status == "In Progress"

        entries = TaskActivity.query.filter_by(task_id=task.id).all()
        assert any("thumbnail needs redoing" in (e.message or "")
                   for e in entries), "the reason belongs on the timeline"


def test_moving_between_ordinary_statuses_needs_no_reason(
        app, client, make_user, make_task, login):
    with app.app_context():
        manager = make_user("admin", permissions=["manage_tasks"])
        task = make_task(manager)          # starts Assigned

        login(manager)
        response = _move(client, task, "In Progress")

        assert response.status_code == 200
        db.session.refresh(task)
        assert task.status == "In Progress"


def test_a_reason_on_an_ordinary_move_is_not_pasted_onto_the_timeline(
        app, client, make_user, make_task, login):
    """Only a move that required a reason should carry one, or the
    timeline fills with explanations nobody asked for."""
    with app.app_context():
        manager = make_user("admin", permissions=["manage_tasks"])
        task = make_task(manager)

        login(manager)
        _move(client, task, "In Progress", reason="pytest-role-stray reason")

        entries = TaskActivity.query.filter_by(task_id=task.id).all()
        assert not any("pytest-role-stray reason" in (e.message or "")
                       for e in entries)
