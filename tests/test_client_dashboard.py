"""Per-client delivery figures.

The app had no per-client aggregate. The one delivery number it showed -
ClientDeliverable.completed_count on the client page - is a denormalised
counter maintained in four separate places and editable by hand, so it can
drift from what actually shipped with nothing to say so.

These pin the two things that make the dashboard worth trusting: that
"delivered" means a task that really reached Published and stayed there, and
that the IST/UTC split in the task timestamps is handled the way the rest of
the codebase handles it. Getting the second wrong moves work between days and
is invisible until someone counts by hand.
"""

from datetime import date, datetime, timedelta

import pytest

from app.extensions import db
from app.models import Client, ClientDeliverable, ClientMonthlyTarget, Task
from app.services import client_dashboard
from app.utils import periods, task_status
from app.utils.timezone import ist_now

PREFIX = "pytest-role-"


@pytest.fixture()
def client_with_work(session, make_user):
    """A client, this month's target with one deliverable, and an assignee."""
    owner = make_user("video_editor")
    today = ist_now().date()

    customer = Client(client_name=PREFIX + "dash client", status="active")
    db.session.add(customer)
    db.session.flush()

    target = ClientMonthlyTarget(client_id=customer.id, month=today.month,
                                 year=today.year)
    db.session.add(target)
    db.session.flush()

    deliverable = ClientDeliverable(
        monthly_target_id=target.id, service_name="Video Editing",
        deliverable_name="Reels", target_count=10, completed_count=0)
    db.session.add(deliverable)
    db.session.flush()
    db.session.commit()

    return customer, deliverable, owner


def _task(customer, deliverable, owner, status, completed_at=None,
          title=PREFIX + "t", deadline=None):
    task = Task(
        title=title, status=status,
        client_id=customer.id, deliverable_id=deliverable.id,
        assigned_to_id=owner.id, created_by_id=owner.id,
        deadline=deadline or (datetime.utcnow() + timedelta(days=3)),
        completed_at=completed_at,
    )
    db.session.add(task)
    db.session.commit()
    return task


def _month(app):
    return periods.resolve_period({"period": "month"})


# ----------------------------------------------------------------------
# What counts as delivered
# ----------------------------------------------------------------------

def test_a_published_task_counts_as_delivered(session, client_with_work):
    customer, deliverable, owner = client_with_work
    _task(customer, deliverable, owner, task_status.PUBLISHED,
          completed_at=ist_now())

    data = client_dashboard.build_dashboard(
        customer, periods.resolve_period({"period": "month"}))

    assert data["delivered"] == 1


def test_a_task_still_in_progress_does_not(session, client_with_work):
    customer, deliverable, owner = client_with_work
    _task(customer, deliverable, owner, task_status.IN_PROGRESS)

    data = client_dashboard.build_dashboard(
        customer, periods.resolve_period({"period": "month"}))

    assert data["delivered"] == 0
    assert data["in_progress"] == 1


def test_published_without_a_completed_stamp_does_not_count(
        app, client_with_work):
    """completed_at is nulled when a manager pulls a task back out of
    Published. Counting on status alone keeps crediting withdrawn work."""
    customer, deliverable, owner = client_with_work
    _task(customer, deliverable, owner, task_status.PUBLISHED,
          completed_at=None)

    data = client_dashboard.build_dashboard(
        customer, periods.resolve_period({"period": "month"}))

    assert data["delivered"] == 0


def test_void_tasks_are_excluded_everywhere(session, client_with_work):
    """The codebase-wide rule - a voided task is not work that happened."""
    customer, deliverable, owner = client_with_work
    _task(customer, deliverable, owner, task_status.VOID)
    _task(customer, deliverable, owner, task_status.IN_PROGRESS)

    data = client_dashboard.build_dashboard(
        customer, periods.resolve_period({"period": "month"}))

    assert data["total_live"] == 1
    assert task_status.VOID not in data["by_status"]


def test_another_clients_work_is_not_counted(session, client_with_work, make_user):
    customer, deliverable, owner = client_with_work

    other = Client(client_name=PREFIX + "other", status="active")
    db.session.add(other)
    db.session.commit()

    _task(customer, deliverable, owner, task_status.PUBLISHED,
          completed_at=ist_now())

    data = client_dashboard.build_dashboard(
        other, periods.resolve_period({"period": "month"}))

    assert data["delivered"] == 0
    assert data["total_live"] == 0


# ----------------------------------------------------------------------
# The IST/UTC split - the most likely bug in the module
# ----------------------------------------------------------------------

def test_a_late_evening_delivery_lands_on_its_own_day(session, client_with_work):
    """completed_at is stamped with ist_now(), so it must be compared with a
    plain date(). Run it through ist_date() as well and everything published
    between IST 00:00 and 05:30 moves to the wrong day."""
    customer, deliverable, owner = client_with_work

    today = ist_now().date()
    late = datetime.combine(today, datetime.min.time()) + timedelta(hours=23,
                                                                    minutes=45)
    _task(customer, deliverable, owner, task_status.PUBLISHED,
          completed_at=late)

    data = client_dashboard.build_dashboard(
        customer, periods.resolve_period({"period": "month"}))

    assert data["delivered"] == 1, "a 23:45 IST delivery fell out of today"


def test_early_hours_delivery_counts_today(session, client_with_work):
    customer, deliverable, owner = client_with_work

    today = ist_now().date()
    early = datetime.combine(today, datetime.min.time()) + timedelta(hours=2)
    _task(customer, deliverable, owner, task_status.PUBLISHED,
          completed_at=early)

    data = client_dashboard.build_dashboard(
        customer, periods.resolve_period({"period": "month"}))

    assert data["delivered"] == 1


# ----------------------------------------------------------------------
# Target vs delivered, and the drift the stored counter can carry
# ----------------------------------------------------------------------

def test_delivery_is_counted_against_the_service_line(session, client_with_work):
    customer, deliverable, owner = client_with_work
    for _ in range(3):
        _task(customer, deliverable, owner, task_status.PUBLISHED,
              completed_at=ist_now())

    data = client_dashboard.build_dashboard(
        customer, periods.resolve_period({"period": "month"}))

    assert len(data["service_lines"]) == 1
    line = data["service_lines"][0]
    assert line["delivered"] == 3
    assert line["target"] == 10
    assert line["percent"] == 30


def test_a_stored_counter_that_disagrees_is_reported_not_hidden(
        app, client_with_work):
    """completed_count is hand-editable. Silently trusting either number is
    how a wrong one survives for months."""
    customer, deliverable, owner = client_with_work
    _task(customer, deliverable, owner, task_status.PUBLISHED,
          completed_at=ist_now())

    deliverable.completed_count = 7          # somebody typed it in
    db.session.commit()

    data = client_dashboard.build_dashboard(
        customer, periods.resolve_period({"period": "month"}))
    line = data["service_lines"][0]

    assert line["delivered"] == 1, "the real count must come from tasks"
    assert line["stored"] == 7
    assert line["drift"] == -6
    assert data["has_drift"] is True


def test_no_target_for_the_month_is_not_an_error(app, session, make_user):
    customer = Client(client_name=PREFIX + "bare", status="active")
    db.session.add(customer)
    db.session.commit()

    data = client_dashboard.build_dashboard(
        customer, periods.resolve_period({"period": "month"}))

    assert data["service_lines"] == []
    assert data["target_total"] == 0
    assert data["target_percent"] is None
    assert data["has_drift"] is False


# ----------------------------------------------------------------------
# Trend
# ----------------------------------------------------------------------

def test_the_trend_zero_fills_quiet_days(session, client_with_work):
    """A chart that skips empty days draws a slope between two deliveries a
    week apart and implies steady output."""
    customer, deliverable, owner = client_with_work
    _task(customer, deliverable, owner, task_status.PUBLISHED,
          completed_at=ist_now())

    period = periods.resolve_period({"period": "month"})
    data = client_dashboard.build_dashboard(customer, period)

    assert len(data["trend"]["labels"]) == period["span_days"]
    assert len(data["trend"]["counts"]) == period["span_days"]
    assert sum(data["trend"]["counts"]) == 1


def test_all_time_has_no_daily_trend(session, client_with_work):
    """An unbounded window has no per-day series to draw."""
    customer, _d, _o = client_with_work

    data = client_dashboard.build_dashboard(
        customer, periods.resolve_period({"period": "all"},
                                         allow_all=True))

    assert data["trend"]["labels"] == []


# ----------------------------------------------------------------------
# The month preset
# ----------------------------------------------------------------------

def test_month_preset_starts_on_the_first(app):
    period = periods.resolve_period({"period": "month"})

    assert period["start"].day == 1
    assert period["label"] == "This month"
    # Month-to-date: counting into a month that has not happened yet only
    # pads the chart with empty future days.
    assert period["end"] <= ist_now().date()


def test_month_compares_against_the_previous_calendar_month(app):
    """Not "the same number of days earlier" - that puts a 31-day month
    against a 30-day one and calls the difference a trend."""
    period = periods.resolve_period({"period": "month"})

    assert period["prev_start"].day == 1
    assert period["prev_end"] == period["start"] - timedelta(days=1)


def test_prev_month_is_the_whole_previous_month(app):
    period = periods.resolve_period({"period": "prev_month"})
    today = ist_now().date()

    assert period["start"].day == 1
    assert period["end"] == today.replace(day=1) - timedelta(days=1)


def test_unknown_period_still_falls_back(app):
    """The existing contract - a hand-typed key must not 500."""
    period = periods.resolve_period({"period": "nonsense"},
                                    default="month")
    assert period["key"] == "month"


# ----------------------------------------------------------------------
# The route
# ----------------------------------------------------------------------

def test_the_dashboard_needs_view_client_stats(
        session, client, make_user, login, client_with_work):
    """The permission has been in the catalog and in three roles' defaults
    since the rebuild, and until this page nothing checked it."""
    customer, _d, _o = client_with_work
    login(make_user("video_editor"))

    response = client.get(f"/clients/{customer.id}/dashboard")

    assert response.status_code == 403


def test_the_permission_opens_it(session, client, make_user, login,
                                 client_with_work):
    customer, _d, _o = client_with_work
    login(make_user("video_editor", permissions=["view_client_stats"]))

    response = client.get(f"/clients/{customer.id}/dashboard")

    assert response.status_code == 200
    assert customer.client_name in response.get_data(as_text=True)


def test_management_sees_it_without_the_explicit_grant(
        session, client, make_user, login, client_with_work):
    customer, _d, _o = client_with_work
    login(make_user("admin"))

    assert client.get(f"/clients/{customer.id}/dashboard").status_code == 200


def test_a_client_with_no_work_gets_an_empty_state(
        session, client, make_user, login):
    """Zero delivered for a client nobody has booked work against reads as a
    failure rather than an absence."""
    login(make_user("admin"))

    customer = Client(client_name=PREFIX + "quiet", status="active")
    db.session.add(customer)
    db.session.commit()

    body = client.get(f"/clients/{customer.id}/dashboard").get_data(as_text=True)

    assert "Nothing to report yet" in body


def test_the_tab_strip_links_both_ways(session, client, make_user, login,
                                       client_with_work):
    customer, _d, _o = client_with_work
    login(make_user("admin"))

    detail = client.get(f"/clients/{customer.id}").get_data(as_text=True)
    dash = client.get(f"/clients/{customer.id}/dashboard").get_data(as_text=True)

    assert f"/clients/{customer.id}/dashboard" in detail
    assert f"/clients/{customer.id}" in dash
    assert "client-tab" in detail and "client-tab" in dash


def test_the_tab_is_hidden_from_someone_who_cannot_open_it(
        session, client, make_user, login, client_with_work):
    """A door to a 403 is worse than no door."""
    customer, _d, _o = client_with_work
    login(make_user("video_editor"))

    detail = client.get(f"/clients/{customer.id}").get_data(as_text=True)

    assert f"/clients/{customer.id}/dashboard" not in detail
