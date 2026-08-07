"""A platform that cannot publish must not strand the post.

Reported: a video post went out on Facebook and could never go out on
Instagram (Instagram takes video as a Reel, not as a video). The list said
"Scheduled", the post page showed one Published and one "Draft" with the
reason buried in a table cell, and there was nothing to click. It would
have stayed that way forever.

Three separate faults, each pinned below:
  * the blocked target was left at `draft` - a lie, draft means nobody
    has submitted it yet
  * the rollup only knew `published` and `failed`, so a post with a
    blocked target could never settle
  * "not supported" said what you cannot do and not what you can
"""

import pytest

from app.extensions import db
from app.models import SocialAccount, SocialPost, SocialPostTarget
from app.social.dto import Capabilities, MediaRef, PostContent
from app.social.media import pipeline
from app.social.queue import worker
from app.social.services import publishing
from app.social.tokens.vault import get_vault

_n = {"i": 0}


def _post(session, statuses, post_status="scheduled"):
    """A post with one target per given status."""
    _n["i"] += 1
    post = SocialPost(title=f"stuck {_n['i']}", status=post_status)
    session.add(post)
    session.flush()

    for j, status in enumerate(statuses, 1):
        account = SocialAccount(
            platform="fake", external_id=f"S{_n['i']}-{j}",
            display_name=f"Chan {j}", account_type="page", status="active",
            token_ciphertext=get_vault().encrypt("AT"), token_key_version=1)
        session.add(account)
        session.flush()
        session.add(SocialPostTarget(
            social_post_id=post.id, social_account_id=account.id,
            platform="fake", post_type="video", caption="hi",
            status=status,
            external_post_id=("EXT" if status == "published" else None)))
    session.flush()
    return post


# --------------------------------------------------------------------------
# "not supported" should point at the way through
# --------------------------------------------------------------------------

def test_a_video_on_instagram_becomes_a_reel_rather_than_a_problem(app):
    """Superseded, and by something better: the app no longer TELLS anyone
    to switch to a Reel, it maps the video onto one itself. So a video
    aimed at Instagram never reaches validation as a "video" at all.

    (This test used to assert the wording of that advice.)
    """
    from app.social.media import fit
    from app.social.providers.meta_instagram import MetaInstagramProvider

    post_type, _ = fit.choose_post_type(
        "video", MetaInstagramProvider.capabilities,
        {"width": 720, "height": 1280, "duration": 30})

    assert post_type == "reel"


def test_an_unsupported_type_lists_what_the_platform_takes(app):
    caps = Capabilities(post_types={"text", "image"})
    content = PostContent(platform="google_business", post_type="carousel")
    problems = pipeline.validate_against(caps, content)

    assert "image" in problems[0] and "text" in problems[0]


# --------------------------------------------------------------------------
# A blocked target says so, instead of pretending to be a draft
# --------------------------------------------------------------------------

def test_a_target_that_cannot_publish_is_marked_blocked(
        session, monkeypatch, app):
    post = _post(session, ["draft"], post_status="approved")
    post.approved_at = db.func.now()
    session.flush()

    monkeypatch.setattr(publishing, "validate_target",
                        lambda t: ["video is not supported on this platform."])

    result = publishing.schedule_post(post)

    target = post.targets[0]
    assert target.status == "blocked", "draft would claim it was never sent"
    assert "not supported" in (target.last_error or "")
    assert result["scheduled"] == 0


def test_a_post_with_nothing_schedulable_is_not_called_scheduled(
        session, monkeypatch, app):
    """Saying "Scheduled" when nothing is queued leaves someone waiting
    for a publish that can never happen."""
    post = _post(session, ["draft"], post_status="approved")
    session.flush()
    monkeypatch.setattr(publishing, "validate_target", lambda t: ["nope"])

    publishing.schedule_post(post)

    assert post.status == "failed"


def test_a_partially_schedulable_post_is_still_scheduled(
        session, monkeypatch, app):
    post = _post(session, ["draft", "draft"], post_status="approved")
    session.flush()

    bad = post.targets[0].id
    monkeypatch.setattr(publishing, "validate_target",
                        lambda t: ["nope"] if t.id == bad else [])

    publishing.schedule_post(post)

    assert post.status == "scheduled"
    assert post.targets[0].status == "blocked"
    assert post.targets[1].status == "scheduled"


# --------------------------------------------------------------------------
# The rollup can settle a post that has a blocked target
# --------------------------------------------------------------------------

def test_published_plus_blocked_settles_as_partially_published(session):
    """The reported case. It used to stay "scheduled" forever, because the
    rollup was waiting on a target that would never move."""
    post = _post(session, ["published", "blocked"])

    worker._maybe_finalize_post(post.targets[0])

    assert post.status == "partially_published"


def test_all_blocked_settles_as_failed(session):
    post = _post(session, ["blocked", "blocked"])
    worker._maybe_finalize_post(post.targets[0])
    assert post.status == "failed"


def test_all_published_is_still_published(session):
    post = _post(session, ["published", "published"])
    worker._maybe_finalize_post(post.targets[0])
    assert post.status == "published"


def test_a_still_pending_target_does_not_settle_the_post(session):
    """One platform live and another genuinely mid-flight is not the same
    as one live and one dead - that post is still in progress."""
    post = _post(session, ["published", "scheduled"])
    worker._maybe_finalize_post(post.targets[0])
    assert post.status == "scheduled"


# --------------------------------------------------------------------------
# is_stuck, including the rows created before `blocked` existed
# --------------------------------------------------------------------------

def test_blocked_and_failed_are_stuck(session):
    post = _post(session, ["blocked", "failed"])
    assert all(t.is_stuck for t in post.targets)


def test_a_draft_target_on_a_live_post_is_stuck(session):
    """Exactly the shape of the reported bug, and of every row created
    before the blocked state existed - it must still be actionable."""
    post = _post(session, ["published", "draft"])
    stuck = [t for t in post.targets if t.is_stuck]
    assert len(stuck) == 1
    assert stuck[0].status == "draft"


def test_a_draft_target_on_a_draft_post_is_not_stuck(session):
    """A post nobody has submitted yet is not stuck - it is just a draft."""
    post = _post(session, ["draft", "draft"], post_status="draft")
    assert not any(t.is_stuck for t in post.targets)


def test_a_published_target_is_not_stuck(session):
    post = _post(session, ["published"])
    assert not post.targets[0].is_stuck


# --------------------------------------------------------------------------
# Dropping the platform is the way out
# --------------------------------------------------------------------------

def test_dropping_the_blocked_platform_settles_the_post(
        session, client, make_user, login, app):
    post = _post(session, ["published", "blocked"])
    blocked = post.targets[1]
    blocked_id, post_id = blocked.id, post.id
    db.session.commit()

    actor = make_user("admin", permissions=["manage_social"])
    login(actor)
    resp = client.post(f"/social/targets/{blocked_id}/drop",
                       follow_redirects=True)
    assert resp.status_code == 200

    assert db.session.get(SocialPostTarget, blocked_id) is None
    refreshed = db.session.get(SocialPost, post_id)
    assert refreshed.status == "published", (
        "with the blocker gone, the one live platform means published")


def test_a_live_platform_cannot_be_dropped(
        session, client, make_user, login, app):
    """Dropping only forgets it here - a published post has to be removed
    from the platform, which is what Remove does."""
    post = _post(session, ["published"])
    target_id = post.targets[0].id
    db.session.commit()

    actor = make_user("admin", permissions=["manage_social"])
    login(actor)
    client.post(f"/social/targets/{target_id}/drop", follow_redirects=True)

    assert db.session.get(SocialPostTarget, target_id) is not None


# --------------------------------------------------------------------------
# Repairing a target that was created before the reel-first mapping
# --------------------------------------------------------------------------

def test_fix_automatically_turns_an_instagram_video_into_a_reel(
        session, client, make_user, login):
    """The one-click repair for the reported post. Editing it was a dead
    end - a scheduled post is not editable - so the fix has to live on the
    target."""
    from app.extensions import db
    from app.models import (SocialAccount, SocialMediaAsset, SocialPost,
                            SocialPostTarget)

    actor = make_user("admin", permissions=["manage_social"])
    login(actor)

    account = SocialAccount(platform="instagram", external_id="RM-1",
                            display_name="ig", account_type="ig_business",
                            status="active")
    db.session.add(account)
    post = SocialPost(title="remap me", status="scheduled")
    db.session.add_all([account, post])
    db.session.flush()
    target = SocialPostTarget(
        social_post_id=post.id, social_account_id=account.id,
        platform="instagram", post_type="video", status="blocked",
        last_error="video is not supported on this platform.")
    db.session.add(target)
    db.session.flush()
    db.session.add(SocialMediaAsset(
        target_id=target.id, source="upload", role="main", sort_order=0,
        object_key="social_uploads/clip.mp4",
        meta={"measurements": {"width": 720, "height": 1280,
                               "duration": 30}}))
    db.session.commit()
    target_id = target.id

    client.post(f"/social/targets/{target_id}/remap", follow_redirects=True)

    fixed = db.session.get(SocialPostTarget, target_id)
    assert fixed.post_type == "reel"
    assert fixed.status == "scheduled"
    assert fixed.last_error is None


def test_fix_automatically_refuses_when_the_file_is_the_problem(
        session, client, make_user, login):
    """A 2-second clip cannot be a reel however it is labelled - and the
    message must say the number, not offer a fix that cannot work."""
    from app.extensions import db
    from app.models import (SocialAccount, SocialMediaAsset, SocialPost,
                            SocialPostTarget)

    actor = make_user("admin", permissions=["manage_social"])
    login(actor)

    account = SocialAccount(platform="instagram", external_id="RM-2",
                            display_name="ig", account_type="ig_business",
                            status="active")
    post = SocialPost(title="too short", status="scheduled")
    db.session.add_all([account, post])
    db.session.flush()
    target = SocialPostTarget(
        social_post_id=post.id, social_account_id=account.id,
        platform="instagram", post_type="video", status="blocked")
    db.session.add(target)
    db.session.flush()
    db.session.add(SocialMediaAsset(
        target_id=target.id, source="upload", role="main", sort_order=0,
        object_key="social_uploads/short.mp4",
        meta={"measurements": {"width": 720, "height": 1280, "duration": 2}}))
    db.session.commit()
    target_id = target.id

    resp = client.post(f"/social/targets/{target_id}/remap",
                       follow_redirects=True)
    body = resp.get_data(as_text=True)

    assert "3s" in body, "the message must name the limit"
    assert db.session.get(SocialPostTarget, target_id).post_type == "video"


def test_a_published_target_cannot_be_remapped(
        session, client, make_user, login):
    from app.extensions import db
    from app.models import SocialPost, SocialPostTarget

    actor = make_user("admin", permissions=["manage_social"])
    login(actor)

    post = SocialPost(title="live", status="published")
    db.session.add(post)
    db.session.flush()
    target = SocialPostTarget(social_post_id=post.id, platform="facebook",
                              post_type="video", status="published",
                              external_post_id="EXT")
    db.session.add(target)
    db.session.commit()
    target_id = target.id

    client.post(f"/social/targets/{target_id}/remap", follow_redirects=True)
    assert db.session.get(SocialPostTarget, target_id).post_type == "video"


def test_the_post_page_offers_a_working_action_not_a_dead_end(
        session, client, make_user, login):
    """"Edit post" on a scheduled post led straight to "This post can no
    longer be edited" - _EDITABLE_STATUSES excludes scheduled. The panel
    must only offer what will actually work."""
    from app.extensions import db
    from app.models import SocialPost

    actor = make_user("admin", permissions=["manage_social"])
    login(actor)

    post = _post(session, ["published", "blocked"], post_status="scheduled")
    db.session.commit()

    body = client.get(f"/social/posts/{post.id}").get_data(as_text=True)

    # A working action is offered. This target is already set to a type its
    # platform supports, so re-deciding the type would change nothing - and
    # "Fix automatically" is now hidden exactly there, which is this test's
    # own rule ("only offer what will actually work") applied to that button.
    # It used to appear on every target, do nothing, and say so only after
    # the click.
    assert "Fix automatically" not in body
    assert "Retry" in body
    assert "Remove platform" in body

    assert f"/posts/{post.id}/edit" not in body, (
        "editing a scheduled post is refused, so the link must not be there")
    # And it says why the whole post cannot be edited.
    assert "already live on" in body


def test_a_stuck_post_with_nothing_live_can_be_reopened(
        session, client, make_user, login):
    """Nothing has published, so taking the whole post back to draft is
    safe - and is the natural way to fix it."""
    from app.extensions import db

    actor = make_user("admin", permissions=["manage_social"])
    login(actor)

    post = _post(session, ["blocked", "blocked"], post_status="scheduled")
    db.session.commit()

    body = client.get(f"/social/posts/{post.id}").get_data(as_text=True)
    assert "Reopen &amp; edit" in body or "Reopen & edit" in body
