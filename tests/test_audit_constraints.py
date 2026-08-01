"""Invariants the application assumed and the database did not enforce.

Each of these was true of the production data on the day it was checked, which
is exactly why they were safe to add and exactly why nobody had noticed they
were missing. A constraint that only ever fires on a bug is doing its job.

Worth recording: adding them broke four test fixtures that had been creating
rows the schema now forbids - tasks with no task_code, publish jobs with no
idempotency key. That is the evidence these were reachable states, not
theoretical ones.
"""

import inspect

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db


# ----------------------------------------------------------------------
# M8 - one daily report per person per day
# ----------------------------------------------------------------------

def test_a_second_report_for_the_same_day_is_refused(session, make_user):
    """add_report get-or-creates on (employee_id, report_date), so without
    this a double-submitted form raced into two rows - and the timesheet then
    listed the day twice and doubled its completed count."""
    from datetime import date

    from app.models import DailyReport

    user = make_user("employee")
    day = date(2026, 7, 1)

    session.add(DailyReport(employee_id=user.id, report_date=day,
                            completed_work="first"))
    session.commit()

    session.add(DailyReport(employee_id=user.id, report_date=day,
                            completed_work="second"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_two_people_may_file_on_the_same_day(session, make_user):
    """The constraint is per person - it must not serialise the whole team."""
    from datetime import date

    from app.models import DailyReport

    a, b = make_user("employee"), make_user("employee")
    day = date(2026, 7, 2)

    session.add_all([
        DailyReport(employee_id=a.id, report_date=day, completed_work="a"),
        DailyReport(employee_id=b.id, report_date=day, completed_work="b"),
    ])
    session.commit()          # must not raise


# ----------------------------------------------------------------------
# M10 - the delivery counters have a floor
# ----------------------------------------------------------------------

@pytest.fixture()
def deliverable(session):
    from app.models import Client, ClientDeliverable, ClientMonthlyTarget

    customer = Client(client_name="pytest-role-ck", status="active")
    session.add(customer)
    session.flush()
    target = ClientMonthlyTarget(client_id=customer.id, month=3, year=2098)
    session.add(target)
    session.flush()
    row = ClientDeliverable(monthly_target_id=target.id, service_name="S",
                            deliverable_name="D", target_count=5,
                            completed_count=0)
    session.add(row)
    session.commit()
    return row


def test_a_negative_completed_count_is_refused(session, deliverable):
    """The max(0, ...) clamp lived in two routes. The client dashboard
    coalesces NULL to 0, so a negative rendered as merely 'behind'."""
    deliverable.completed_count = -1
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_a_negative_target_count_is_refused(session, deliverable):
    deliverable.target_count = -3
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_a_null_count_is_refused(session, deliverable):
    deliverable.completed_count = None
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_zero_is_still_allowed(session, deliverable):
    """The floor is zero, not one - a deliverable with nothing delivered yet
    is the normal state at the start of a month."""
    deliverable.completed_count = 0
    session.commit()

    assert deliverable.completed_count == 0


# ----------------------------------------------------------------------
# M12 / L8 - unique-but-nullable is not unique
# ----------------------------------------------------------------------

def test_a_job_without_an_idempotency_key_is_refused(session, make_target):
    """Postgres permits unlimited NULLs in a unique index, so a key-less job
    silently escaped the 'never publish the same target twice for one
    schedule' guarantee the class docstring makes."""
    from app.models import PublishJob
    from datetime import datetime

    _acct, _post, target = make_target()

    session.add(PublishJob(target_id=target.id, state="queued", attempts=0,
                           max_attempts=5, next_run_at=datetime.utcnow()))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_a_task_without_a_code_is_refused(session, make_user):
    """R2 object keys are built from task_code; two NULLs would fall back to
    the same path and collide."""
    from datetime import datetime, timedelta

    from app.models import Client, ClientDeliverable, ClientMonthlyTarget, Task

    owner = make_user("employee")
    customer = Client(client_name="pytest-role-nocode", status="active")
    session.add(customer)
    session.flush()
    target = ClientMonthlyTarget(client_id=customer.id, month=4, year=2098)
    session.add(target)
    session.flush()
    deliv = ClientDeliverable(monthly_target_id=target.id, service_name="S",
                              deliverable_name="D", target_count=1,
                              completed_count=0)
    session.add(deliv)
    session.flush()

    session.add(Task(title="pytest-role-nocode", status="Assigned",
                     client_id=customer.id, deliverable_id=deliv.id,
                     assigned_to_id=owner.id, created_by_id=owner.id,
                     deadline=datetime.utcnow() + timedelta(days=1)))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ----------------------------------------------------------------------
# M9 - a status with no time bucket says so
# ----------------------------------------------------------------------

def test_every_status_has_a_duration_bucket():
    """The failure this prevents already happened once, with "Hold": a status
    the app knows about but DURATION_FIELD does not silently discards every
    second spent in it. Caught here rather than in someone's timesheet."""
    from app.utils import task_status

    missing = [s for s in task_status.ALL_STATUSES
               if not task_status.duration_field(s)]

    assert not missing, (
        "%s in ALL_STATUSES with no DURATION_FIELD entry - time spent in "
        "them is dropped without a word" % missing)


def test_an_unmapped_status_is_logged_not_swallowed():
    """The other way in: a literal written straight to the database, which no
    test can catch ahead of time."""
    from app.routes import tasks

    source = inspect.getsource(tasks.record_status_time)

    assert "logger.warning" in source, (
        "duration_field() returns None quietly and the branch just skips, so "
        "without this the time goes missing with no signal at all"
    )


# ----------------------------------------------------------------------
# M6 - the rate lock is not held across ffmpeg
# ----------------------------------------------------------------------

def test_the_rate_reservation_commits_before_the_slow_work():
    """reserve() holds SELECT ... FOR UPDATE on the budget row until the
    transaction ends. Committing only at the dispatch marker meant that lock
    spanned a token refresh, a media download and an ffmpeg transcode."""
    from app.social.queue import worker

    source = inspect.getsource(worker._process)

    after_reserve = source.split("provider_state[\"_reserved\"] = True")[1]
    before_slow = after_reserve.split("AccountManager.access_token")[0]

    assert "db.session.commit()" in before_slow, (
        "the budget row lock is still held across build_content() and "
        "transcode.fit_content(), so concurrent publishes for one account "
        "serialise behind ffmpeg with an idle-in-transaction lock"
    )


def test_the_reserved_flag_is_persisted_with_the_commit():
    """Committing early is only safe if the flag goes with it - otherwise a
    crash before dispatch loses the flag and the retry reserves twice."""
    from app.social.queue import worker

    source = inspect.getsource(worker._process)

    block = source.split("provider_state[\"_reserved\"] = True")[1]
    block = block.split("db.session.commit()")[0]

    assert "job.provider_state" in block


# ----------------------------------------------------------------------
# M7 - a blocked target can be retried
# ----------------------------------------------------------------------

def test_blocked_counts_as_retryable():
    from app.social.services import lifecycle

    assert "blocked" in lifecycle.STUCK_TARGET_STATUSES
    assert "failed" in lifecycle.STUCK_TARGET_STATUSES


@pytest.mark.parametrize("where", ["social", "tasks"])
def test_both_retry_paths_accept_blocked(where):
    """A blocked target was refused before sending, for a reason the composer
    can fix - so it is the most retryable state there is, and it was the one
    both retry paths skipped."""
    if where == "social":
        from app.routes.social import retry_target as fn
    else:
        from app.routes.tasks import retry_task_publish as fn

    source = inspect.getsource(fn)

    assert "STUCK_TARGET_STATUSES" in source


def test_the_retry_button_is_offered_for_blocked():
    """The route fix is unreachable if the template still hides the button."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "templates" / "social" / "post_detail.html").read_text(
        encoding="utf-8", errors="ignore")

    assert "t.status in ['failed', 'blocked']" in html
