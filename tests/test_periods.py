"""Date windows behind the ?period= picker.

One resolver serves the dashboard's Performance band and the user
performance page, so these pin both the shared maths and the one thing
that differs between them: All time is opt-in per page.
"""

from datetime import timedelta

import pytest

from app.utils import periods
from app.utils.timezone import ist_now


@pytest.fixture()
def today(app):
    with app.app_context():
        return ist_now().date()


def _resolve(args=None, **kwargs):
    return periods.resolve_period(args or {}, **kwargs)


# --------------------------------------------------------------------------
# The presets
# --------------------------------------------------------------------------

def test_today_is_a_single_day(app, today):
    with app.app_context():
        p = _resolve({"period": "today"})
    assert p["start"] == p["end"] == today
    assert p["span_days"] == 1
    assert p["label"] == "Today"


def test_yesterday_is_the_day_before(app, today):
    with app.app_context():
        p = _resolve({"period": "yesterday"})
    assert p["start"] == p["end"] == today - timedelta(days=1)
    assert p["span_days"] == 1


def test_seven_days_includes_today(app, today):
    """Inclusive at both ends: "last 7 days" is 7 days of work, not 8."""
    with app.app_context():
        p = _resolve({"period": "7d"})
    assert p["end"] == today
    assert p["start"] == today - timedelta(days=6)
    assert p["span_days"] == 7


def test_thirty_days_includes_today(app, today):
    with app.app_context():
        p = _resolve({"period": "30d"})
    assert p["end"] == today
    assert p["start"] == today - timedelta(days=29)
    assert p["span_days"] == 30


# --------------------------------------------------------------------------
# Custom ranges
# --------------------------------------------------------------------------

def test_a_custom_range_is_read_as_given(app):
    with app.app_context():
        p = _resolve({"period": "custom",
                      "from": "2026-03-02", "to": "2026-03-11"})
    assert p["from"] == "2026-03-02"
    assert p["to"] == "2026-03-11"
    assert p["span_days"] == 10


def test_a_backwards_range_is_read_the_way_it_was_meant(app):
    """Picking the two dates in the wrong order is a slip, not a request
    to see nothing."""
    with app.app_context():
        p = _resolve({"period": "custom",
                      "from": "2026-03-11", "to": "2026-03-02"})
    assert p["from"] == "2026-03-02"
    assert p["to"] == "2026-03-11"


def test_an_enormous_range_is_clamped(app):
    """Keeps the dashboard's per-day loop bounded however wide a range
    somebody types in."""
    with app.app_context():
        p = _resolve({"period": "custom",
                      "from": "2015-01-01", "to": "2026-01-01"})
    assert p["span_days"] == periods.MAX_PERIOD_DAYS
    assert p["to"] == "2026-01-01"


def test_a_malformed_custom_date_falls_back_to_a_sane_window(app, today):
    with app.app_context():
        p = _resolve({"period": "custom", "from": "not-a-date", "to": ""})
    assert p["start"] == today - timedelta(days=6)
    assert p["end"] == today


# --------------------------------------------------------------------------
# All time is opt-in
# --------------------------------------------------------------------------

def test_all_time_is_unbounded_when_offered(app):
    with app.app_context():
        p = _resolve({"period": "all"}, allow_all=True, default="all")
    assert p["is_all_time"] is True
    assert p["start"] is None and p["end"] is None
    assert p["label"] == "All time"


def test_all_time_carries_no_previous_window(app):
    """There is no "period before everything" to compare against."""
    with app.app_context():
        p = _resolve({"period": "all"}, allow_all=True, default="all")
    assert p["prev_start"] is None and p["prev_end"] is None
    assert p["span_days"] is None


def test_all_time_cannot_be_reached_where_it_is_not_offered(app, today):
    """The dashboard counts per day across the window - an unbounded one
    has no answer, so hand-typing ?period=all must not reach it."""
    with app.app_context():
        p = _resolve({"period": "all"})
    assert p["is_all_time"] is False
    assert p["key"] == "7d"
    assert p["start"] == today - timedelta(days=6)


# --------------------------------------------------------------------------
# Defaults and junk
# --------------------------------------------------------------------------

def test_no_period_uses_the_callers_default(app):
    with app.app_context():
        assert _resolve({})["key"] == "7d"
        assert _resolve({}, allow_all=True, default="all")["key"] == "all"


def test_an_unknown_period_falls_back_to_the_callers_default(app):
    """Not to a hard-coded 7d - a page defaulting to All time should stay
    on All time when handed nonsense."""
    with app.app_context():
        assert _resolve({"period": "zzz"})["key"] == "7d"
        assert _resolve({"period": "zzz"},
                        allow_all=True, default="all")["key"] == "all"


# --------------------------------------------------------------------------
# The previous window, which the deltas are drawn against
# --------------------------------------------------------------------------

def test_the_previous_window_is_the_same_span_immediately_before(app):
    with app.app_context():
        p = _resolve({"period": "7d"})
    assert p["prev_end"] == p["start"] - timedelta(days=1)
    assert (p["prev_end"] - p["prev_start"]).days + 1 == p["span_days"]


# --------------------------------------------------------------------------
# The performance page actually honours the window
# --------------------------------------------------------------------------

def _perf(client, user, query=""):
    return client.get(
        f"/users/{user.id}/performance{query}"
    ).get_data(as_text=True)


def test_the_performance_page_scopes_its_figures_to_the_window(
        app, client, make_user, make_task, login):
    from app.extensions import db

    with app.app_context():
        manager = make_user("admin", permissions=["manage_users"])
        worker = make_user("video_editor")

        old = make_task(worker, title="pytest-role-ancient work")
        recent = make_task(worker, title="pytest-role-todays work")
        old.created_at = ist_now() - timedelta(days=200)
        db.session.commit()

        login(manager)

        all_time = _perf(client, worker, "?period=all")
        assert "pytest-role-ancient work" in all_time
        assert "pytest-role-todays work" in all_time

        just_today = _perf(client, worker, "?period=today")
        assert "pytest-role-ancient work" not in just_today
        assert "pytest-role-todays work" in just_today


def test_the_performance_page_defaults_to_all_time(
        app, client, make_user, make_task, login):
    from app.extensions import db

    with app.app_context():
        manager = make_user("admin", permissions=["manage_users"])
        worker = make_user("video_editor")
        old = make_task(worker, title="pytest-role-long ago")
        old.created_at = ist_now() - timedelta(days=200)
        db.session.commit()

        login(manager)
        body = _perf(client, worker)

    assert "All time" in body
    assert "pytest-role-long ago" in body
