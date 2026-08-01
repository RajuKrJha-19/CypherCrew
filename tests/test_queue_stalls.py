"""Three ways a post sat in the queue and nobody could tell why.

All three were reported as one symptom - "Facebook chala jaata hai, Instagram
aur YouTube pe jaa hi nahi raha" - and none of them is a publishing failure.

**The rollup gap.** A post whose targets ended up ["removed", "failed"] matched
no branch in either rollup, so it kept `partially_published` forever AND
delete_post refuses exactly that status. Unsettleable and undeletable, with the
task pinned in Published behind it.

**The stale clock.** `locked_at` was stamped at claim time and never touched
again, so the 20-minute abandon window had to cover token refresh, media
download and an ffmpeg transcode before the upload even started. A large
YouTube upload crossed it while succeeding, `_reset_stale` requeued it, and the
dispatch marker then turned the resume into an interrupted publish - a video
that uploaded fine came back as "may already be live, check before retrying".

**The silent defer.** YouTube allows six uploads per 24 hours. Past that every
attempt re-defers 30 minutes with nothing written anywhere, which from the
outside is indistinguishable from a publisher that does not work.
"""

import inspect
from datetime import datetime, timedelta

import pytest

from app.social.queue import worker
from app.social.services import lifecycle


# ----------------------------------------------------------------------
# post_status_from - the shared rollup
# ----------------------------------------------------------------------

@pytest.mark.parametrize("statuses,expected", [
    (["published"], "published"),
    (["published", "published"], "published"),
    (["removed"], "removed"),
    (["removed", "removed"], "removed"),
    (["failed"], "failed"),
    (["failed", "blocked"], "failed"),
    (["blocked"], "failed"),
    (["published", "failed"], "partially_published"),
    (["published", "blocked"], "partially_published"),
    # A target that published and was later taken down is a publish plus a
    # removal, not a partial publish.
    (["published", "removed"], "published"),
])
def test_settled_combinations(statuses, expected):
    assert lifecycle.post_status_from(statuses) == expected


def test_the_combination_that_matched_nothing():
    """Remove the live half of a partially-published post. Before the fix this
    returned no status at all and the post was stuck forever."""
    assert lifecycle.post_status_from(["removed", "failed"]) == "failed"
    assert lifecycle.post_status_from(["failed", "removed"]) == "failed"
    assert lifecycle.post_status_from(["removed", "blocked"]) == "failed"


@pytest.mark.parametrize("statuses", [
    ["queued"],
    ["published", "queued"],
    ["scheduled", "failed"],
    ["publishing", "removed"],
])
def test_in_flight_returns_none(statuses):
    """None means "not settled yet" - the caller must leave the post alone
    rather than rolling it up early."""
    assert lifecycle.post_status_from(statuses) is None


def test_no_targets_is_not_settled():
    assert lifecycle.post_status_from([]) is None


def test_every_terminal_combination_gets_an_answer():
    """The old rollups enumerated combinations by hand and missed one. This
    walks the whole space so a future status cannot fall through silently."""
    from itertools import combinations_with_replacement

    terminal = lifecycle.TERMINAL_TARGET_STATUSES
    for size in (1, 2, 3):
        for combo in combinations_with_replacement(terminal, size):
            assert lifecycle.post_status_from(list(combo)) is not None, combo


def test_both_rollups_use_the_one_function():
    """Two rollups that each enumerate their own combinations is how the gap
    appeared in the first place."""
    assert "post_status_from" in inspect.getsource(lifecycle._rollup_removed)
    assert "post_status_from" in inspect.getsource(
        worker._maybe_finalize_post)


# ----------------------------------------------------------------------
# delete_post decides from the targets
# ----------------------------------------------------------------------

def test_delete_reads_the_targets_not_the_stale_status():
    """Posts already stranded in production still carry the wrong status
    string. Refusing on that alone leaves them undeletable forever."""
    from app.routes import social

    source = inspect.getsource(social.delete_post)

    assert 't.status for t in post.targets' in source
    assert 'post.status in ("publishing"' not in source, (
        "still refusing on the status string, which is the stale value"
    )


# ----------------------------------------------------------------------
# delete_post, driven for real
#
# The source-inspection test above cannot see behaviour, and that is not a
# theoretical gap: the first version of this fix derived "busy" from "not
# settled yet", which is true of every draft, and quietly made ordinary drafts
# undeletable. Every case below goes through the route.
# ----------------------------------------------------------------------

def _delete(client, post):
    return client.post("/social/posts/%d/delete" % post.id,
                       follow_redirects=False)


@pytest.fixture()
def publisher(make_user, login, client):
    # admin qualifies for Social Studio by role (can_use_social), so no
    # explicit permission row is needed.
    user = make_user("admin")
    login(user)
    return client


@pytest.mark.parametrize("target_status", [
    "draft", "pending_approval", "approved", "scheduled",
    "failed", "blocked", "removed", "rejected",
])
def test_a_post_nothing_is_doing_can_be_deleted(
        publisher, make_target, session, target_status):
    """draft is the one that matters most - it is what this route is for -
    but nothing in this list is live or in flight."""
    from app.models import SocialPost

    _acct, post, target = make_target()
    target.status = target_status
    session.commit()
    post_id = post.id

    response = _delete(publisher, post)

    assert response.status_code == 302
    assert "/drafts" in response.headers["Location"], (
        "%s was refused - it is neither live nor in flight" % target_status
    )
    assert SocialPost.query.get(post_id) is None


def test_a_live_post_cannot_be_deleted(publisher, make_target, session):
    from app.models import SocialPost

    _acct, post, target = make_target()
    target.status = "published"
    session.commit()
    post_id = post.id

    response = _delete(publisher, post)

    assert "/drafts" not in response.headers.get("Location", "")
    assert SocialPost.query.get(post_id) is not None


def test_a_post_mid_publish_cannot_be_deleted(publisher, make_target, session):
    from app.models import SocialPost

    _acct, post, target = make_target()
    target.status = "publishing"
    session.commit()
    post_id = post.id

    response = _delete(publisher, post)

    assert "/drafts" not in response.headers.get("Location", "")
    assert SocialPost.query.get(post_id) is not None


def test_the_stranded_post_can_finally_be_deleted(
        publisher, make_target, session):
    """removed + failed: the combination that matched no rollup branch, so the
    post kept `partially_published` - a status delete_post used to refuse."""
    from app.models import SocialPost, SocialPostTarget

    _acct, post, target = make_target()
    target.status = "removed"
    session.add(SocialPostTarget(
        social_post_id=post.id, social_account_id=target.social_account_id,
        platform="fake", post_type="image", caption="hi", status="failed"))
    post.status = "partially_published"     # the stale value it was stuck at
    session.commit()
    post_id = post.id

    response = _delete(publisher, post)

    assert "/drafts" in response.headers["Location"]
    assert SocialPost.query.get(post_id) is None


def test_a_post_with_no_targets_can_be_deleted(publisher, session):
    """A bare draft nobody has picked platforms for yet."""
    from app.models import SocialPost

    post = SocialPost(title="bare", base_caption="c", status="draft")
    session.add(post)
    session.commit()
    post_id = post.id

    response = _delete(publisher, post)

    assert "/drafts" in response.headers["Location"]
    assert SocialPost.query.get(post_id) is None


# ----------------------------------------------------------------------
# The stale clock
# ----------------------------------------------------------------------

def test_the_stale_clock_restarts_when_the_upload_starts():
    """Otherwise the window covers everything before the upload too, and a
    slow-but-alive upload is requeued and then killed as interrupted."""
    source = inspect.getsource(worker._process)

    dispatch = source.split('provider_state["dispatched"] = True')[1]
    dispatch = dispatch.split("start_publish")[0]

    assert "job.locked_at" in dispatch, (
        "locked_at is not refreshed at dispatch, so _STALE_CLAIM_MINUTES is "
        "measured from the claim rather than from the publish call"
    )


def test_reset_stale_skips_locked_rows():
    """Several gunicorn workers run this tick at once."""
    source = inspect.getsource(worker._reset_stale)

    assert "with_for_update" in source
    assert "skip_locked=True" in source


def test_reset_stale_still_only_sweeps_claimed():
    """Deliberate, and worth pinning: `publishing` is assigned in memory but
    _apply_step always overwrites it before the commit, so no job ever reaches
    the database in that state. Sweeping for it would be dead code dressed up
    as a safety net."""
    source = inspect.getsource(worker._reset_stale)

    assert 'PublishJob.state == "claimed"' in source
    assert '"publishing"' not in source.split('"""')[2]


# ----------------------------------------------------------------------
# The rate defer says something
# ----------------------------------------------------------------------

def test_the_defer_message_names_the_limit_and_the_next_attempt():
    when = datetime(2026, 8, 1, 9, 0)  # UTC -> 14:30 IST

    message = worker._rate_defer_message("youtube", 6, 24 * 3600, when)

    assert "YouTube" in message, (
        "platform.capitalize() renders 'Youtube' - PLATFORM_LABELS has the "
        "real casing and this sentence is meant to be believed"
    )
    assert "6 per 24 hours" in message
    assert "14:30" in message, "next attempt not shown in IST"
    assert "not failed" in message, (
        "a deferred post is not a broken one and the wording has to say so"
    )


def test_the_defer_message_handles_a_short_window():
    message = worker._rate_defer_message("instagram", 25, 3600, datetime.utcnow())

    assert "25 per 1 hour" in message


def test_the_defer_branch_records_the_reason():
    """It used to return "rate_deferred" and write nothing anywhere, so the
    post simply sat there."""
    source = inspect.getsource(worker._process)

    branch = source.split("if not ratelimit.reserve(")[1].split("return")[0]

    assert "job.last_error" in branch
    assert "target.last_error" in branch, (
        "the post detail page reads target.last_error - without it, the "
        "person chasing the late post still sees no reason"
    )


def test_the_queue_page_shows_a_waiting_note():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    template = (root / "app" / "templates" / "social" / "queue.html").read_text(
        encoding="utf-8", errors="ignore")

    assert "qc-note" in template
    assert "job.state == 'queued'" in template, (
        "the note only rendered for failed/dead jobs, which is exactly the "
        "set a rate-deferred job is not in"
    )


def test_the_waiting_note_is_styled_and_not_red():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    css = (root / "app" / "static" / "css" / "style.css").read_text(
        encoding="utf-8", errors="ignore")

    rule = css.split(".queue-card .qc-note{")[1].split("}")[0]

    assert "color-danger" not in rule, (
        "nothing has failed - red sends people hunting for a fault that is "
        "not there"
    )


def test_a_deferred_job_is_not_counted_as_failed():
    """The pill on the queue card keys off state, and the deferred job stays
    'queued' - so it must not be styled as a failure."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    template = (root / "app" / "templates" / "social" / "queue.html").read_text(
        encoding="utf-8", errors="ignore")

    pill = template.split("<span class=\"sp-pill")[1].split(">")[0]

    assert "'danger' if job.state in ['failed','dead']" in pill
