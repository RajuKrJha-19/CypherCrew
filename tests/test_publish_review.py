"""The look before the leap: what publishing will do, said before it does it.

Publishing was a single unguarded click. What actually went out - the post type
each platform resolved to, the caption as it would be sent, each channel's own
publish instant, and whether a channel would be refused outright - was computed
AFTER the click and reported as a flash. The person publishing found out what
they had published by reading the result.

Two things are under test here, and the second matters more than the first:

  * build_review answers those questions from the same functions the publish
    path uses, so the review cannot promise one thing and the publish do
    another.

  * the fingerprint makes the review mean something. A modal the client can
    skip is decoration; worse, a review answered after someone else edited the
    post in another tab publishes something nobody saw.
"""

from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import PublishJob, SocialMediaAsset, SocialPostTarget
from app.social.services import publish_review


def _post_with(make_target, **kw):
    _acct, post, target = make_target(**kw)
    return post, target


# ----------------------------------------------------------------------
# fingerprint - what counts as "this is still the post I reviewed"
# ----------------------------------------------------------------------

def test_fingerprint_is_stable_for_an_untouched_post(app, make_target):
    """It has to survive the round trip, or every honest confirmation is
    refused and the feature is just an obstacle."""
    with app.app_context():
        post, _ = _post_with(make_target)
        assert (publish_review.fingerprint(post, "schedule", "")
                == publish_review.fingerprint(post, "schedule", ""))


def test_fingerprint_moves_when_a_caption_changes(app, make_target):
    with app.app_context():
        post, target = _post_with(make_target)
        before = publish_review.fingerprint(post, "schedule", "")

        target.caption = "something entirely different"
        db.session.commit()

        assert publish_review.fingerprint(post, "schedule", "") != before


def test_fingerprint_moves_when_a_channel_time_changes(app, make_target):
    with app.app_context():
        post, target = _post_with(make_target)
        before = publish_review.fingerprint(post, "schedule", "")

        target.scheduled_for = datetime.utcnow() + timedelta(days=3)
        db.session.commit()

        assert publish_review.fingerprint(post, "schedule", "") != before


def test_fingerprint_moves_when_a_channel_is_added(app, make_target):
    """The case that costs the most: you review two channels, someone adds a
    third, you confirm - and publish to a channel you never saw."""
    with app.app_context():
        post, target = _post_with(make_target)
        before = publish_review.fingerprint(post, "schedule", "")

        extra = SocialPostTarget(
            social_post_id=post.id,
            social_account_id=target.social_account_id,
            platform=target.platform, post_type="image", caption="hi",
            status="draft", scheduled_for=target.scheduled_for,
        )
        db.session.add(extra)
        db.session.commit()
        db.session.refresh(post)

        assert publish_review.fingerprint(post, "schedule", "") != before


def test_fingerprint_binds_the_publish_mode(app, make_target):
    """A review read as "Schedule for Friday" must not be confirmable as
    "Publish now" - they are different actions with the same button."""
    with app.app_context():
        post, _ = _post_with(make_target)

        assert (publish_review.fingerprint(post, "now", "")
                != publish_review.fingerprint(post, "schedule", ""))


def test_fingerprint_binds_the_typed_time(app, make_target):
    with app.app_context():
        post, _ = _post_with(make_target)

        assert (publish_review.fingerprint(post, "schedule", "2026-08-01T10:00")
                != publish_review.fingerprint(post, "schedule", "2026-09-09T10:00"))


# ----------------------------------------------------------------------
# The gate on schedule_post
# ----------------------------------------------------------------------

def test_publishing_without_a_fingerprint_is_refused(
        session, client, make_user, login, make_target):
    """The server is the gate; the modal is only the UI."""
    login(make_user("admin", permissions=["manage_social"]))
    _acct, post, target = make_target(when_past=False)
    post.status = "approved"
    db.session.commit()

    response = client.post(f"/social/posts/{post.id}/schedule",
                           data={"publish_mode": "now"},
                           follow_redirects=False)

    assert response.status_code in (302, 303)
    db.session.refresh(post)
    assert post.status == "approved", "it published without a review"
    assert PublishJob.query.filter_by(target_id=target.id).count() == 0


def test_a_stale_fingerprint_is_refused(
        session, client, make_user, login, make_target):
    """Reviewed, then edited elsewhere, then confirmed. This is the whole
    reason the fingerprint exists."""
    login(make_user("admin", permissions=["manage_social"]))
    _acct, post, target = make_target(when_past=False)
    post.status = "approved"
    db.session.commit()

    reviewed = publish_review.fingerprint(post, "now", "")

    # Somebody rewrites the caption in another tab.
    target.caption = "a completely different message"
    db.session.commit()

    response = client.post(f"/social/posts/{post.id}/schedule",
                           data={"publish_mode": "now",
                                 "review_fingerprint": reviewed},
                           follow_redirects=False)

    assert response.status_code in (302, 303)
    db.session.refresh(post)
    assert post.status == "approved"
    assert PublishJob.query.filter_by(target_id=target.id).count() == 0


def test_a_matching_fingerprint_publishes(
        session, client, make_user, login, make_target):
    login(make_user("admin", permissions=["manage_social"]))
    _acct, post, target = make_target(when_past=False)
    post.status = "approved"
    db.session.commit()

    response = client.post(
        f"/social/posts/{post.id}/schedule",
        data={"publish_mode": "now",
              "review_fingerprint": publish_review.fingerprint(post, "now", "")},
        follow_redirects=False)

    assert response.status_code in (302, 303)
    db.session.refresh(target)
    assert target.scheduled_for is not None
    assert target.status in ("scheduled", "publishing", "blocked")


# ----------------------------------------------------------------------
# build_review - what the modal is told
# ----------------------------------------------------------------------

def test_publish_now_marks_every_channel_immediate(app, make_target):
    with app.app_context():
        post, _ = _post_with(make_target, when_past=False)

        review = publish_review.build_review(post, publish_mode="now")

        assert review["publish_now"] is True
        assert all(c["immediate"] for c in review["channels"])


def test_scheduled_channels_keep_their_own_times(app, session, make_target):
    """Channels can and do publish at different times. A review that showed
    one of them would be lying about the rest."""
    with app.app_context():
        _acct, post, target = make_target(when_past=False)

        later = datetime.utcnow() + timedelta(hours=9)
        second = SocialPostTarget(
            social_post_id=post.id,
            social_account_id=target.social_account_id,
            platform=target.platform, post_type="image", caption="hi",
            status="draft", scheduled_for=later,
        )
        db.session.add(second)
        db.session.commit()
        db.session.refresh(post)

        review = publish_review.build_review(post, publish_mode="schedule")

        assert review["staggered"] is True, "divergent times not reported"
        whens = {c["when"] for c in review["channels"]}
        assert len(whens) == 2


def test_a_typed_time_overrides_every_channel(app, make_target):
    with app.app_context():
        post, _ = _post_with(make_target, when_past=False)
        chosen = datetime.utcnow() + timedelta(days=2)

        review = publish_review.build_review(
            post, publish_mode="schedule", schedule_override=chosen)

        assert all(c["when"] == chosen for c in review["channels"])
        assert review["staggered"] is False


def test_times_are_reported_in_ist(app, make_target):
    """The team schedules in IST; a review quoting UTC would be a worse answer
    than no review."""
    with app.app_context():
        post, _ = _post_with(make_target, when_past=False)

        review = publish_review.build_review(post, publish_mode="schedule")
        channel = review["channels"][0]

        assert (channel["when_ist"] - channel["when"]
                == publish_review.IST_OFFSET)


def test_a_channel_that_cannot_publish_is_reported_with_its_reason(
        app, session, make_target):
    """The point of the whole feature. Today one bad channel schedules the
    good ones and blocks itself, and you learn which from a flash afterwards."""
    with app.app_context():
        _acct, post, target = make_target(when_past=False)

        # No account selected - validate_target refuses it outright.
        target.social_account_id = None
        db.session.commit()

        review = publish_review.build_review(post, publish_mode="now")
        channel = review["channels"][0]

        assert channel["outcome"] == publish_review.WILL_BLOCK
        assert channel["reasons"], "blocked with no reason given"
        assert review["blocked_count"] == 1
        assert review["publishing_count"] == 0
        assert review["nothing_to_publish"] is True


def test_a_healthy_channel_is_reported_as_publishing(app, make_target):
    with app.app_context():
        post, _ = _post_with(make_target, when_past=False)

        review = publish_review.build_review(post, publish_mode="now")

        assert review["channels"][0]["outcome"] == publish_review.WILL_PUBLISH
        assert review["blocked_count"] == 0
        assert review["nothing_to_publish"] is False


def test_the_caption_shown_is_the_one_that_will_be_sent(app, session,
                                                        make_target):
    """Per-channel overrides mean the post's base caption is not necessarily
    what any given channel gets."""
    with app.app_context():
        _acct, post, target = make_target(when_past=False)
        target.caption = "the override that actually goes out"
        db.session.commit()

        review = publish_review.build_review(post, publish_mode="now")

        assert review["channels"][0]["caption"] == (
            "the override that actually goes out")
        assert review["channels"][0]["caption_len"] == len(target.caption)


def test_a_post_with_no_channels_reviews_cleanly(app, session, make_target):
    with app.app_context():
        _acct, post, target = make_target(when_past=False)
        SocialMediaAsset.query.filter_by(target_id=target.id).delete()
        db.session.delete(target)
        db.session.commit()
        db.session.refresh(post)

        review = publish_review.build_review(post, publish_mode="now")

        assert review["channels"] == []
        assert review["nothing_to_publish"] is True


# ----------------------------------------------------------------------
# The route
# ----------------------------------------------------------------------

def test_the_review_route_returns_the_fragment(
        session, client, make_user, login, make_target):
    login(make_user("admin", permissions=["manage_social"]))
    _acct, post, target = make_target(when_past=False)
    post.status = "approved"
    db.session.commit()

    response = client.get(f"/social/posts/{post.id}/review?publish_mode=now")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'class="pubrev"' in body
    # It is a fragment, not a page - inserted, never navigated to.
    assert "<html" not in body.lower()
    assert publish_review.fingerprint(post, "now", "") in body


def test_the_review_route_needs_studio_access(
        session, client, make_user, login, make_target):
    login(make_user("video_editor"))
    _acct, post, _t = make_target(when_past=False)

    response = client.get(f"/social/posts/{post.id}/review", follow_redirects=False)

    assert response.status_code in (302, 303, 403)


def test_the_schedule_form_carries_the_review_hooks():
    """Both halves, or the review never runs: the form has to be marked, and
    the script that reads the mark has to be loaded."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    detail = (root / "app" / "templates" / "social" / "post_detail.html"
              ).read_text(encoding="utf-8", errors="ignore")
    shell = (root / "app" / "templates" / "base_studio.html"
             ).read_text(encoding="utf-8", errors="ignore")

    assert "data-publish-review" in detail
    assert 'name="review_fingerprint"' in detail
    assert "publish-review.js" in shell


def test_the_script_gates_in_capture_phase_and_survives_turbo():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "publish-review.js").read_text(encoding="utf-8",
                                               errors="ignore")

    assert "preventDefault" in source
    assert "stopPropagation" in source, (
        "without it Turbo submits the form anyway and the review is bypassed"
    )
    assert "turbo:before-render" in source, (
        "a stray overlay would hang over the next page"
    )
    assert "media-viewer.show" in source, (
        "the media viewer owns Escape while it is open"
    )
