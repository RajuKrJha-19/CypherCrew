"""Three findings from the second audit pass.

**The wrong clock, twice.** Task.deadline is naive IST wall-clock (it comes
from a datetime-local input) and employee_completed_at is stamped with
ist_now(). No TZ is set in config.py, Procfile or gunicorn.conf.py, so the
server runs on UTC. Comparing either against utcnow()/date.today() is off by
5h30m - which showed up as the team panel calling a task on-time while the
task list called it overdue, and as a daily report filed before IST 05:30
landing on yesterday.

**A feature nobody could reach.** `_capabilities_map()` feeds the composer, and
it never emitted `story_support`. The composer gates the "Also share to Story"
checkbox on exactly that key, so the lookup was always undefined and the box
was permanently disabled - behind it a complete, tested backend: companion
Story target creation, story_style, story_link_done, needs_story_link.
"""

import inspect

import pytest

from app.utils.timezone import IST_OFFSET


# ----------------------------------------------------------------------
# H8 - team workload overdue
# ----------------------------------------------------------------------

def test_workload_overdue_uses_the_ist_clock():
    from app.routes import dashboard

    source = inspect.getsource(dashboard.team_workload) \
        if hasattr(dashboard, "team_workload") else None

    if source is None:
        # The panel lives inside a larger view; fall back to the module and
        # assert the specific line is gone.
        source = inspect.getsource(dashboard)

    assert "now = datetime.utcnow()\n    workload" not in source, (
        "the workload panel is back on the UTC clock, so it disagrees with "
        "every other overdue test in the app by 5h30m"
    )


def test_every_deadline_comparison_in_dashboard_uses_ist():
    """Task.deadline is IST wall-clock. A utcnow() anywhere near it is a bug,
    so this pins the whole module rather than one line."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    source = (root / "app" / "routes" / "dashboard.py").read_text(
        encoding="utf-8", errors="ignore")

    for i, line in enumerate(source.splitlines(), 1):
        code = line.split("#", 1)[0]
        if "deadline" in code and "utcnow" in code:
            pytest.fail("dashboard.py:%d compares a deadline to UTC: %s"
                        % (i, line.strip()))


# ----------------------------------------------------------------------
# H7 - the report day boundary
# ----------------------------------------------------------------------

def test_add_report_uses_the_ist_day():
    from app.routes import reports

    source = inspect.getsource(reports.add_report)

    code = "\n".join(l.split("#", 1)[0] for l in source.splitlines())

    assert "ist_now().date()" in code
    assert "date.today()" not in code, (
        "between IST 00:00 and 05:30 this stamps the report with yesterday "
        "and pre-fills yesterday's completed tasks"
    )


def test_no_route_defaults_a_period_from_the_server_date():
    """The month/year pickers on reports and clients both defaulted from the
    server clock, so on the 1st of a month before IST 05:30 they opened on
    last month."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in ("reports.py", "clients.py"):
        source = (root / "app" / "routes" / name).read_text(
            encoding="utf-8", errors="ignore")
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "date.today()" not in stripped, (
                "%s:%d still defaults from the server date: %s"
                % (name, i, stripped))


def test_ist_is_actually_ahead():
    """The whole class of bug depends on this being non-zero and positive; if
    the offset were ever zeroed these tests would pass vacuously."""
    assert IST_OFFSET.total_seconds() == 5.5 * 3600


# ----------------------------------------------------------------------
# H6 - the Story checkbox
# ----------------------------------------------------------------------

@pytest.fixture()
def caps(app):
    from app.routes.social import _capabilities_map

    with app.app_context():
        return _capabilities_map()


def test_story_support_reaches_the_composer(caps):
    """The one missing key. Without it storyPlatforms() returns [] for every
    platform and the checkbox can never be ticked."""
    if not caps:
        pytest.skip("no social providers registered in this configuration")

    assert all("story_support" in v for v in caps.values())


def test_at_least_one_platform_offers_a_story(caps):
    """If this is empty the checkbox is still dead, just for a new reason."""
    if not caps:
        pytest.skip("no social providers registered in this configuration")

    assert any(v["story_support"] for v in caps.values()), (
        "no platform reports story_support, so the checkbox stays disabled"
    )


def test_story_support_matches_the_server_side_gate(caps):
    """schedule_post creates the companion target only when story_support AND
    'story' in post_types. If the composer used a looser test, the box would
    tick and silently produce no Story."""
    if not caps:
        pytest.skip("no social providers registered in this configuration")

    for key, value in caps.items():
        if value["story_support"]:
            assert "story" in value["post_types"], (
                "%s advertises story_support to the composer but the server "
                "would refuse to create the target" % key
            )


def test_the_composer_still_reads_the_key_this_emits():
    """A rename on either side re-breaks the feature silently, which is
    exactly how it broke the first time."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    compose = (root / "app" / "templates" / "social" / "compose.html").read_text(
        encoding="utf-8", errors="ignore")

    assert "story_support" in compose
    assert "storyPlatforms" in compose


def test_the_two_gates_are_written_the_same_way():
    """One vocabulary, both sides - the map and schedule_post."""
    from app.routes import social

    emitted = inspect.getsource(social._capabilities_map)
    gate = inspect.getsource(social._apply_composer_form)

    for fragment in ("caps.story_support", '"story" in (caps.post_types'):
        assert fragment in emitted, fragment
        assert fragment in gate, fragment


# ----------------------------------------------------------------------
# H3 - the task list's N+1
#
# publish_badge() is rendered twice per row (the table row and the board
# card), and each call issued a fresh SocialPost query plus a lazy load of
# that post's targets. The list has no pagination, so the cost grew with the
# table forever. These count real queries rather than reading the source,
# because "is this still one query" is not something source can answer.
# ----------------------------------------------------------------------

class _QueryCounter:
    """Counts SELECTs on the shared engine for the duration of a block."""

    def __init__(self):
        self.n = 0

    def __enter__(self):
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        self._handler = lambda conn, cur, stmt, p, ctx, many: (
            setattr(self, "n", self.n + 1)
            if stmt.lstrip().upper().startswith("SELECT") else None
        )
        event.listen(Engine, "before_cursor_execute", self._handler)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        event.remove(Engine, "before_cursor_execute", self._handler)
        return False


@pytest.fixture()
def social_tasks(app, make_task, make_user, session):
    """Eight social tasks, each with a Studio post carrying two targets."""
    from app.models import SocialPost, SocialPostTarget

    owner = make_user("social_media_executive")
    tasks = []
    for i in range(8):
        task = make_task(owner, title="pytest-role-social-%d" % i)
        task.is_social_media = True
        session.flush()

        post = SocialPost(title="p%d" % i, base_caption="c", status="draft",
                          task_id=task.id)
        session.add(post)
        session.flush()
        for platform in ("facebook", "instagram"):
            session.add(SocialPostTarget(
                social_post_id=post.id, platform=platform,
                post_type="image", caption="hi", status="draft"))
        tasks.append(task)

    session.commit()
    return tasks


def test_priming_collapses_the_per_task_queries(app, social_tasks):
    """Two badge renders per task, eight tasks - the shape the list has."""
    from app.social.services import task_link

    with app.test_request_context("/tasks/"):
        session_tasks = social_tasks
        with _QueryCounter() as unprimed:
            for t in session_tasks:
                task_link.publish_badge(t)
                task_link.publish_badge(t)

    with app.test_request_context("/tasks/"):
        with _QueryCounter() as primed:
            task_link.prime_badges(social_tasks)
            for t in social_tasks:
                task_link.publish_badge(t)
                task_link.publish_badge(t)

    assert unprimed.n > primed.n, (
        "priming saved nothing: %d queries unprimed, %d primed"
        % (unprimed.n, primed.n))
    assert primed.n <= 3, (
        "priming should be a constant couple of queries for the whole page, "
        "got %d" % primed.n)


def test_priming_does_not_change_what_the_badge_says(app, social_tasks):
    """A cache that returns something different is worse than the N+1."""
    from app.social.services import task_link

    with app.test_request_context("/tasks/"):
        before = [task_link.publish_badge(t) for t in social_tasks]

    with app.test_request_context("/tasks/"):
        task_link.prime_badges(social_tasks)
        after = [task_link.publish_badge(t) for t in social_tasks]

    assert before == after


def test_the_cache_is_opt_in(app, social_tasks):
    """Nothing outside a primed list render may read it - the worker and the
    publish path must never see a stale batch."""
    from app.social.services import task_link

    with app.test_request_context("/tasks/"):
        # No prime_badges() call: linked_posts must go to the database.
        with _QueryCounter() as counter:
            task_link.linked_posts(social_tasks[0])

        assert counter.n >= 1


def test_priming_outside_a_request_is_a_no_op(app, social_tasks):
    """The worker calls linked_posts() with no request context."""
    from app.social.services import task_link

    with app.app_context():
        task_link.prime_badges(social_tasks)      # must not raise
        assert task_link.linked_posts(social_tasks[0]) is not None


def test_the_list_route_primes(app):
    import inspect

    from app.routes import tasks

    assert "prime_badges" in inspect.getsource(tasks.list_tasks), (
        "the list route stopped priming, so the N+1 is back"
    )
