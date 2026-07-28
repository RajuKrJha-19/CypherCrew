"""Searching the review queue.

The queue is the one screen you arrive at already knowing which task you
want, so it searches server-side across all three columns rather than
filtering the cards already rendered - Published is capped, and a
client-side filter could never reach past the cap.
"""

from urllib.parse import quote

from app.extensions import db
from app.models import Task


def _stage(task, status, title=None, assignee=None):
    task.status = status
    if title:
        task.title = title
    if assignee:
        task.assigned_to_id = assignee.id
    db.session.commit()


def _queue(client, q=None):
    url = "/my-tasks" + (f"?q={quote(q)}" if q else "")
    return client.get(url).get_data(as_text=True)


def test_the_queue_can_be_searched_by_task_title(
        app, client, make_user, make_task, login):
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        worker = make_user("junior_video_editor")

        wanted = make_task(worker, title="pytest-role-needle task")
        other = make_task(worker, title="pytest-role-haystack task")
        _stage(wanted, "Core Review")
        _stage(other, "Core Review")

        login(reviewer)

        body = _queue(client, "needle")
        assert "pytest-role-needle task" in body
        assert "pytest-role-haystack task" not in body


def test_the_queue_can_be_searched_by_task_id(
        app, client, make_user, make_task, login):
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        worker = make_user("junior_video_editor")

        wanted = make_task(worker, title="pytest-role-by-code")
        other = make_task(worker, title="pytest-role-not-this-one")
        wanted.task_code = 987654
        other.task_code = 111222
        _stage(wanted, "Core Review")
        _stage(other, "Core Review")

        login(reviewer)

        body = _queue(client, str(wanted.task_code))
        assert "pytest-role-by-code" in body
        assert "pytest-role-not-this-one" not in body


def test_the_queue_can_be_searched_by_employee(
        app, client, make_user, make_task, login):
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        mine = make_user("junior_video_editor", name="Zainab Testperson")
        theirs = make_user("junior_graphic_designer", name="Other Person")

        wanted = make_task(mine, title="pytest-role-zainabs work")
        other = make_task(theirs, title="pytest-role-someone elses work")
        _stage(wanted, "Core Review")
        _stage(other, "Core Review")

        login(reviewer)

        body = _queue(client, "Zainab")
        assert "pytest-role-zainabs work" in body
        assert "pytest-role-someone elses work" not in body


def test_search_reaches_every_column(
        app, client, make_user, make_task, login):
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        worker = make_user("junior_video_editor")

        core = make_task(worker, title="pytest-role-shared core")
        clientr = make_task(worker, title="pytest-role-shared client")
        published = make_task(worker, title="pytest-role-shared published")
        _stage(core, "Core Review")
        _stage(clientr, "Client Review")
        _stage(published, "Published")

        login(reviewer)
        body = _queue(client, "shared")

        assert "pytest-role-shared core" in body
        assert "pytest-role-shared client" in body
        assert "pytest-role-shared published" in body


def test_a_search_with_no_hits_says_so(
        app, client, make_user, make_task, login):
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        worker = make_user("junior_video_editor")
        task = make_task(worker, title="pytest-role-present")
        _stage(task, "Core Review")

        login(reviewer)
        body = _queue(client, "zzz-no-such-task")

        assert "Nothing in the review queue matches" in body
        assert "pytest-role-present" not in body


def test_an_empty_search_shows_the_whole_queue(
        app, client, make_user, make_task, login):
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        worker = make_user("junior_video_editor")
        a = make_task(worker, title="pytest-role-first")
        b = make_task(worker, title="pytest-role-second")
        _stage(a, "Core Review")
        _stage(b, "Client Review")

        login(reviewer)
        body = _queue(client)

        assert "pytest-role-first" in body
        assert "pytest-role-second" in body
        assert "Nothing in the review queue matches" not in body


def test_a_task_with_no_assignee_still_matches(
        app, client, make_user, make_task, login):
    """apply_task_search outer-joins for exactly this reason: an inner
    join drops every task whose joined row is missing, however well the
    title matches."""
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        worker = make_user("junior_video_editor")

        orphan = make_task(worker, title="pytest-role-unassigned task")
        orphan.assigned_to_id = None
        _stage(orphan, "Core Review")

        login(reviewer)
        assert "pytest-role-unassigned task" in _queue(client, "unassigned")


def test_search_is_case_insensitive(
        app, client, make_user, make_task, login):
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        worker = make_user("junior_video_editor", name="Kabir Testperson")
        task = make_task(worker, title="pytest-role-MixedCase Needle")
        _stage(task, "Core Review")

        login(reviewer)

        for query in ("mixedcase", "MIXEDCASE", "MiXeDcAsE", "kabir", "KABIR"):
            assert "pytest-role-MixedCase Needle" in _queue(client, query), (
                f"{query!r} should have matched"
            )


def test_every_word_may_match_a_different_column(
        app, client, make_user, make_task, login):
    """The whole point of splitting the query into words.

    "kavya 4242" names one task exactly, but the name lives on User and
    the number on Task - as a single LIKE over the whole string it could
    never match anything, which read as the search being broken.
    """
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        kavya = make_user("junior_video_editor", name="Kavya Testperson")
        other = make_user("junior_graphic_designer", name="Rohit Testperson")

        wanted = make_task(kavya, title="pytest-role-cross column hit")
        wanted.task_code = 424242
        decoy = make_task(other, title="pytest-role-cross column miss")
        decoy.task_code = 999111
        _stage(wanted, "Core Review")
        _stage(decoy, "Core Review")

        login(reviewer)
        body = _queue(client, "kavya 424242")

        assert "pytest-role-cross column hit" in body
        assert "pytest-role-cross column miss" not in body


def test_word_order_does_not_matter(
        app, client, make_user, make_task, login):
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        worker = make_user("junior_video_editor")
        task = make_task(worker, title="pytest-role-alpha beta gamma")
        _stage(task, "Core Review")

        login(reviewer)

        for query in ("alpha gamma", "gamma alpha", "beta alpha"):
            assert "pytest-role-alpha beta gamma" in _queue(client, query), (
                f"{query!r} should have matched"
            )


def test_all_words_must_match_not_just_one(
        app, client, make_user, make_task, login):
    """AND, not OR: adding a word has to narrow the results, or typing
    more of what you remember would only ever make the list longer."""
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        worker = make_user("junior_video_editor")
        task = make_task(worker, title="pytest-role-narrowing target")
        _stage(task, "Core Review")

        login(reviewer)

        assert "pytest-role-narrowing target" in _queue(client, "narrowing")
        assert "pytest-role-narrowing target" not in _queue(
            client, "narrowing zzzabsent"
        )


def test_like_wildcards_are_searched_for_literally(
        app, client, make_user, make_task, login):
    """Unescaped, "%" matched every task in the queue and "_" matched any
    single character - both are ordinary characters to paste in from a
    title."""
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        # Named explicitly: the fixture's default name is built from the
        # role, and "junior_video_editor" would put a literal underscore
        # in a searched column and match the "_" probe honestly.
        worker = make_user("junior_video_editor", name="Plain Testperson")
        plain = make_task(worker, title="pytest-role-plain title")
        percent = make_task(worker, title="pytest-role-50% off campaign")
        _stage(plain, "Core Review")
        _stage(percent, "Core Review")

        login(reviewer)

        body = _queue(client, "%")
        assert "pytest-role-50% off campaign" in body
        assert "pytest-role-plain title" not in body

        assert "pytest-role-plain title" not in _queue(client, "_")


def test_a_bare_hash_does_not_become_a_wildcard(
        app, client, make_user, make_task, login):
    """"#" is stripped so "#1012" finds task 1012, which left an empty
    needle matching every task code when the word was only "#"."""
    with app.app_context():
        reviewer = make_user("admin", permissions=["approve_tasks"])
        worker = make_user("junior_video_editor")
        task = make_task(worker, title="pytest-role-hash guard")
        task.task_code = 707070
        _stage(task, "Core Review")

        login(reviewer)

        assert "pytest-role-hash guard" not in _queue(client, "#")
        assert "pytest-role-hash guard" in _queue(client, "#707070")
