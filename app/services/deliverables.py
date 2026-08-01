"""The one place `ClientDeliverable.completed_count` moves.

Before this there were four writers - the task status machine, the approve
button, the Social Studio publish worker and the deliverable edit form - and
every one of them did the same unsafe thing:

    d.completed_count = (d.completed_count or 0) + 1

which is a read, a modify and a write with nothing holding the row in between.
Two managers approving two tasks on the same deliverable in the same second
both read 5 and both write 6, so one delivery silently disappears from the
client dashboard. The publish worker made it worse rather than rarer: it runs
in a thread pool inside *every* gunicorn worker, and `_maybe_finalize_post`
takes a row lock on SocialPost only - the Task and the ClientDeliverable it
then mutates were unlocked.

There is no way to detect this after the fact. The count and the tasks that
produced it drift apart, the dashboard's drift banner lights up, and nothing
in the data says which of the two numbers is right.

So: `SELECT ... FOR UPDATE` before the read, and the clamp inside the lock.
Serialising these is cheap - a deliverable is touched a few times a month, not
a few times a second.
"""

from app.extensions import db
from app.models import ClientDeliverable


def lock(deliverable_id):
    """Take the deliverable's row lock and return the row.

    Returns None for a missing id, and for `None` itself - a task with no
    deliverable is normal (ad-hoc work), not an error, and every caller here
    would otherwise need the same guard.
    """
    if not deliverable_id:
        return None

    # populate_existing() is not optional here. Without it SQLAlchemy returns
    # the instance already in the identity map and leaves its attributes
    # alone, so the row would be locked in the database while the count we
    # then read came from before the lock - the exact stale read the lock
    # exists to prevent, and it would look like it worked.
    return (
        db.session.query(ClientDeliverable)
        .filter_by(id=deliverable_id)
        .populate_existing()
        .with_for_update()
        .first()
    )


def adjust_count(deliverable_id, delta):
    """Move completed_count by `delta`, under the row lock. Returns the new
    value, or None when there was no deliverable to move.

    The `max(0, ...)` lives here rather than at the call sites so a decrement
    can be unconditional. The status machine used to guard its decrement with
    `if deliverable.completed_count:` - skipping the subtraction when the
    stored count was already 0 while still clearing `completed_at`, which
    netted +1 for every rework-and-republish cycle. With the clamp inside the
    lock the guard is unnecessary, and leaving it out is what makes the cycle
    balance.
    """
    if not delta:
        return None

    row = lock(deliverable_id)
    if row is None:
        return None

    row.completed_count = max(0, (row.completed_count or 0) + delta)
    return row.completed_count


def set_count(deliverable_id, value):
    """Set completed_count outright, under the row lock.

    For the deliverable edit form, which submits an absolute number a human
    typed rather than a delta. It still has to take the lock: without it a
    publish landing mid-edit is overwritten by whatever the form had on
    screen, which is the same lost update from the other direction.
    """
    row = lock(deliverable_id)
    if row is None:
        return None

    row.completed_count = max(0, value or 0)
    return row.completed_count


def move(from_deliverable_id, to_deliverable_id):
    """Carry one delivery from one deliverable to another.

    For retargeting an already-delivered task. `edit_task` reassigns
    `deliverable_id` freely, and nothing used to compensate either counter -
    so moving a Published task from "Reels" to "Statics" left Reels +1 and
    Statics -1 permanently, with no code path able to heal it.

    Locks in ascending id order. Two people retargeting tasks in opposite
    directions between the same two deliverables would otherwise each hold the
    lock the other needs.
    """
    if from_deliverable_id == to_deliverable_id:
        return

    for deliverable_id in sorted(
        [i for i in (from_deliverable_id, to_deliverable_id) if i]
    ):
        lock(deliverable_id)

    adjust_count(from_deliverable_id, -1)
    adjust_count(to_deliverable_id, +1)
