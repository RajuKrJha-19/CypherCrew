"""Date and time on task results in global search.

The whole point of these tests is the timezone split, which is invisible
in the code and a 5.5-hour error if anyone gets it wrong:

  * `Task.deadline` is written straight from a `datetime-local` input -
    it is the wall clock the user typed, already IST. It must NOT be
    shifted. (`tasks/list.html` renders it with a bare `strftime` for
    exactly this reason.)
  * `Task.created_at` defaults to `datetime.utcnow`, so it MUST be
    shifted.

Both end up in the same field of the same JSON payload, which is
precisely why a future reader is likely to "tidy up" by running both
through `|ist`. These tests fail loudly if they do.
"""

from datetime import datetime, timedelta

from app.routes.search import _task_when
from app.utils.timezone import IST_OFFSET


class _Task:
    """Only the two attributes _task_when reads."""

    def __init__(self, deadline=None, created_at=None):
        self.deadline = deadline
        self.created_at = created_at


def test_deadline_is_shown_as_typed_and_never_shifted():
    # 6pm IST, exactly as it came out of the datetime-local input.
    task = _Task(deadline=datetime(2026, 7, 30, 18, 0))

    label, stamp = _task_when(task)

    assert label == "Due"
    assert stamp == "30 Jul 2026, 6:00 PM"
    # The failure this guards: +5:30 would read 11:30 PM, and a deadline
    # near midnight would move to the following day.
    assert "11:30" not in stamp


def test_created_at_is_shifted_from_utc_to_ist():
    # 18:00 UTC is 23:30 IST the same evening.
    task = _Task(created_at=datetime(2026, 7, 30, 18, 0))

    label, stamp = _task_when(task)

    assert label == "Created"
    assert stamp == "30 Jul 2026, 11:30 PM"


def test_created_at_shift_can_cross_midnight():
    """20:00 UTC is 01:30 IST the NEXT day - the case that proves the
    date is recomputed after the shift and not just the clock."""
    task = _Task(created_at=datetime(2026, 7, 30, 20, 0))

    _, stamp = _task_when(task)

    assert stamp == "31 Jul 2026, 1:30 AM"


def test_the_deadline_wins_when_a_task_has_both():
    task = _Task(
        deadline=datetime(2026, 7, 30, 18, 0),
        created_at=datetime(2026, 1, 1, 0, 0),
    )

    label, _ = _task_when(task)

    assert label == "Due"


def test_a_task_with_neither_yields_nothing_to_render():
    """Both renderers gate on a truthy `when`, so this is what keeps an
    empty separator out of the row rather than a bare label."""
    assert _task_when(_Task()) == ("", "")


def test_noon_and_midnight_are_not_rendered_as_zero_or_twelve_wrong():
    """`hour % 12` is 0 at both noon and midnight; the `or 12` is what
    stops "0:30 PM"."""
    assert _task_when(_Task(deadline=datetime(2026, 7, 30, 12, 30)))[1] \
        == "30 Jul 2026, 12:30 PM"
    assert _task_when(_Task(deadline=datetime(2026, 7, 30, 0, 30)))[1] \
        == "30 Jul 2026, 12:30 AM"


def test_suggest_carries_the_stamp_for_every_task_it_returns(
    app, client, login, make_user, make_task,
):
    """End to end: the field has to survive into the JSON the dropdown
    reads, for EVERY task row - the user asked for it on all of them, not
    just the ones that happen to have a deadline."""
    manager = make_user("admin", permissions=["manage_tasks"])
    # The title MUST keep the `pytest-role-` prefix: _purge_test_rows
    # matches on it, and a task it fails to match is left behind holding a
    # foreign key that wedges the cleanup for every later run.
    task = make_task(manager, title="pytest-role-stamp-task")

    login(manager)
    response = client.get("/search/suggest?q=pytest-role-stamp-task")

    assert response.status_code == 200

    groups = {group["type"]: group for group in response.get_json()["groups"]}
    assert "task" in groups, "the task did not come back from search at all"

    items = groups["task"]["items"]
    assert items

    for item in items:
        assert item["when"], f"{item['title']} came back with no date"
        assert item["when_label"] in ("Due", "Created")

    match = next(i for i in items if i["title"] == "pytest-role-stamp-task")
    # make_task sets a deadline, and it is stored unshifted.
    assert match["when_label"] == "Due"
    assert match["when"] == (
        f"{task.deadline:%d %b %Y}, "
        f"{task.deadline.hour % 12 or 12}:{task.deadline:%M %p}"
    )


def test_the_stamp_is_formatted_on_the_server_not_in_the_browser():
    """The renderer prints `item.when` verbatim. If someone starts
    shipping a raw ISO timestamp instead, the dropdown silently shows
    `2026-07-30T18:00:00` - readable enough in review to pass, wrong
    enough in the UI to matter."""
    _, stamp = _task_when(_Task(deadline=datetime(2026, 7, 30, 18, 0)))

    assert "T" not in stamp
    assert stamp.endswith("PM")


def test_ist_offset_is_the_shift_these_tests_assume():
    """The expected strings above are hard-coded to +5:30. If the offset
    constant ever changes, fail here with a clear reason rather than in
    six opaque string comparisons."""
    assert IST_OFFSET == timedelta(hours=5, minutes=30)
