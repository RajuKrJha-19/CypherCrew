"""ClientDeliverable.completed_count - one writer, under a row lock.

The audit found four separate writers, all doing

    d.completed_count = (d.completed_count or 0) + 1

with nothing holding the row between the read and the write, and one of them
running from the publish worker's thread pool in every gunicorn worker. Two
approvals in the same second both read 5 and both write 6.

Three separate arithmetic bugs sat on top of that:

  * `approve_task` incremented with no `completed_at` guard, so a
    double-clicked Approve counted twice;
  * the rework path guarded its DECREMENT with `if completed_count:` while
    clearing completed_at unconditionally, so a rework-and-republish cycle
    netted +1 with no delivery;
  * `edit_task` reassigned `deliverable_id` with no compensation, so a
    retargeted delivery left one line permanently +1 and the other -1.

None of these leave a trace. The count and the tasks that produced it drift
apart and nothing in the data says which number is right, which is why they
are tested here rather than left to the dashboard's drift banner.
"""

from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import Client, ClientDeliverable, ClientMonthlyTarget, Task
from app.services import deliverables
from app.utils import task_status
from app.utils.timezone import ist_now

PREFIX = "pytest-role-"


def _a_code():
    """tasks.task_code is unique and NOT NULL; a test row still needs one."""
    import random
    return random.randint(10_000_000, 99_999_999)


@pytest.fixture()
def two_lines(session, make_user):
    """A client with two deliverables - "Reels" and "Statics" - so a delivery
    can be moved between them."""
    owner = make_user("video_editor")
    today = ist_now().date()

    customer = Client(client_name=PREFIX + "count client", status="active")
    db.session.add(customer)
    db.session.flush()

    target = ClientMonthlyTarget(client_id=customer.id, month=today.month,
                                 year=today.year)
    db.session.add(target)
    db.session.flush()

    reels = ClientDeliverable(
        monthly_target_id=target.id, service_name="Video Editing",
        deliverable_name="Reels", target_count=10, completed_count=0)
    statics = ClientDeliverable(
        monthly_target_id=target.id, service_name="Design",
        deliverable_name="Statics", target_count=10, completed_count=0)
    db.session.add_all([reels, statics])
    db.session.flush()
    db.session.commit()

    return customer, reels, statics, owner


def _task(customer, deliverable, owner, status=task_status.CLIENT_REVIEW):
    task = Task(
        title=PREFIX + "counted", status=status,
        task_code=_a_code(),
        client_id=customer.id, deliverable_id=deliverable.id,
        assigned_to_id=owner.id, created_by_id=owner.id,
        deadline=datetime.utcnow() + timedelta(days=3),
        quantity=1, estimated_time=1,
    )
    db.session.add(task)
    db.session.commit()
    return task


# ----------------------------------------------------------------------
# The helper
# ----------------------------------------------------------------------

def test_adjust_moves_the_count(two_lines):
    _customer, reels, _statics, _owner = two_lines

    assert deliverables.adjust_count(reels.id, +1) == 1
    assert deliverables.adjust_count(reels.id, +1) == 2
    assert deliverables.adjust_count(reels.id, -1) == 1


def test_the_clamp_lives_inside_the_helper(two_lines):
    """So a decrement can be unconditional at every call site. The guard this
    replaces (`if completed_count:`) is what made a rework cycle net +1."""
    _customer, reels, _statics, _owner = two_lines

    assert deliverables.adjust_count(reels.id, -1) == 0
    assert deliverables.adjust_count(reels.id, -5) == 0


def test_no_deliverable_is_not_an_error(two_lines):
    """Ad-hoc work with no deliverable is normal, and every call site would
    otherwise repeat the same None guard."""
    assert deliverables.adjust_count(None, +1) is None
    assert deliverables.adjust_count(0, +1) is None
    assert deliverables.lock(None) is None


def test_a_zero_delta_touches_nothing(two_lines):
    _customer, reels, _statics, _owner = two_lines

    assert deliverables.adjust_count(reels.id, 0) is None


def test_set_count_clamps_too(two_lines):
    _customer, reels, _statics, _owner = two_lines

    assert deliverables.set_count(reels.id, 7) == 7
    assert deliverables.set_count(reels.id, -3) == 0
    assert deliverables.set_count(reels.id, None) == 0


def test_move_carries_one_delivery_across(two_lines):
    _customer, reels, statics, _owner = two_lines
    deliverables.set_count(reels.id, 3)
    deliverables.set_count(statics.id, 1)

    deliverables.move(reels.id, statics.id)
    db.session.commit()

    assert reels.completed_count == 2
    assert statics.completed_count == 2


def test_move_to_the_same_deliverable_is_a_no_op(two_lines):
    _customer, reels, _statics, _owner = two_lines
    deliverables.set_count(reels.id, 4)

    deliverables.move(reels.id, reels.id)

    assert reels.completed_count == 4


def test_move_from_nothing_still_credits_the_target(two_lines):
    """A task that had no deliverable and gains one. There is nothing to take
    away, but the delivery it already made now belongs somewhere."""
    _customer, reels, _statics, _owner = two_lines

    deliverables.move(None, reels.id)

    assert reels.completed_count == 1


def test_the_helper_takes_a_row_lock():
    """The whole point. A plain SELECT here would still pass every arithmetic
    test above while leaving the lost update wide open."""
    import inspect

    source = inspect.getsource(deliverables.lock)

    assert "with_for_update" in source
    assert "populate_existing" in source, (
        "without populate_existing SQLAlchemy hands back the identity-mapped "
        "instance unrefreshed, so the row is locked while the count read "
        "comes from before the lock"
    )


# ----------------------------------------------------------------------
# The status machine
# ----------------------------------------------------------------------

def test_publishing_counts_one(two_lines):
    from app.routes.tasks import apply_completion_effects, record_status_time

    customer, reels, _statics, owner = two_lines
    task = _task(customer, reels, owner)

    record_status_time(task, task_status.PUBLISHED)
    apply_completion_effects(task, task_status.PUBLISHED)
    db.session.commit()

    assert reels.completed_count == 1
    assert task.completed_at is not None


def test_re_entering_published_does_not_count_twice(two_lines):
    from app.routes.tasks import apply_completion_effects, record_status_time

    customer, reels, _statics, owner = two_lines
    task = _task(customer, reels, owner)

    for _ in range(3):
        record_status_time(task, task_status.PUBLISHED)
        apply_completion_effects(task, task_status.PUBLISHED)
    db.session.commit()

    assert reels.completed_count == 1


def test_a_rework_cycle_nets_zero(two_lines):
    """Publish, pull back for rework, publish again. Before the fix the
    decrement was skipped whenever the stored count was 0 while completed_at
    was cleared anyway, so each cycle added one delivery that never happened."""
    from app.routes.tasks import apply_completion_effects, record_status_time

    customer, reels, _statics, owner = two_lines
    task = _task(customer, reels, owner)

    for _ in range(3):
        record_status_time(task, task_status.PUBLISHED)
        apply_completion_effects(task, task_status.PUBLISHED)
        record_status_time(task, task_status.CLIENT_REVIEW)
        apply_completion_effects(task, task_status.CLIENT_REVIEW)
    db.session.commit()

    assert reels.completed_count == 0
    assert task.completed_at is None


def test_rework_from_a_hand_zeroed_count_does_not_invent_a_delivery(two_lines):
    """Somebody edits the count to 0 by hand, then a manager reworks and
    republishes. The old guard returned 1 here - a delivery from nothing."""
    from app.routes.tasks import apply_completion_effects, record_status_time

    customer, reels, _statics, owner = two_lines
    task = _task(customer, reels, owner)

    record_status_time(task, task_status.PUBLISHED)
    apply_completion_effects(task, task_status.PUBLISHED)
    db.session.commit()

    deliverables.set_count(reels.id, 0)
    db.session.commit()

    record_status_time(task, task_status.CLIENT_REVIEW)
    apply_completion_effects(task, task_status.CLIENT_REVIEW)
    record_status_time(task, task_status.PUBLISHED)
    apply_completion_effects(task, task_status.PUBLISHED)
    db.session.commit()

    assert reels.completed_count == 1


# ----------------------------------------------------------------------
# Retargeting
# ----------------------------------------------------------------------

def test_retargeting_a_delivered_task_moves_its_count(two_lines):
    """The bug with no cure: before this, Reels stayed +1 and Statics -1
    forever, and no code path could reconcile them."""
    from app.routes.tasks import apply_completion_effects, record_status_time

    customer, reels, statics, owner = two_lines
    task = _task(customer, reels, owner)

    record_status_time(task, task_status.PUBLISHED)
    apply_completion_effects(task, task_status.PUBLISHED)
    db.session.commit()
    assert (reels.completed_count, statics.completed_count) == (1, 0)

    deliverables.move(task.deliverable_id, statics.id)
    task.deliverable_id = statics.id
    db.session.commit()

    assert (reels.completed_count, statics.completed_count) == (0, 1)


def test_retargeting_an_undelivered_task_moves_nothing(two_lines):
    """It never counted, so there is nothing to carry - and crediting the new
    line here would invent a delivery."""
    customer, reels, statics, owner = two_lines
    task = _task(customer, reels, owner)

    assert task.completed_at is None
    task.deliverable_id = statics.id
    db.session.commit()

    assert (reels.completed_count, statics.completed_count) == (0, 0)


# ----------------------------------------------------------------------
# The call sites still route through the helper
# ----------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "app/routes/tasks.py",
    "app/routes/clients.py",
    "app/social/services/task_link.py",
])
def test_no_writer_touches_the_column_directly(path):
    """A fifth writer added later would silently reopen every bug above."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    source = Path(root / path).read_text(encoding="utf-8", errors="ignore")

    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert ".completed_count =" not in stripped, (
            "%s writes completed_count directly instead of going through "
            "app.services.deliverables: %s" % (path, stripped)
        )


def test_the_worker_locks_the_task_before_stamping():
    """C4: the caller locks the SocialPost only, so without this two posts
    finishing for one task both saw completed_at as NULL."""
    import inspect

    from app.social.services import task_link

    source = inspect.getsource(task_link)

    assert "with_for_update" in source
    assert "populate_existing" in source


def test_approve_guards_on_completed_at():
    """A double-clicked Approve arrives while the first request is still in
    flight, so "already Published" is not yet true anywhere it can see."""
    import inspect

    from app.routes import tasks

    source = inspect.getsource(tasks.approve_task)

    assert "if not task.completed_at:" in source
