"""Stories that are supposed to open a feed post.

Instagram's Content Publishing API takes image_url/video_url for a
STORIES container and nothing else - no sticker, no link - so a story
asked to open a post publishes as a plain story and the sticker is added
by hand in the app afterwards. These tests pin the two halves of that:
the intent survives from the composer to the target row, and the manual
follow-up can be seen and closed.
"""

from datetime import datetime

from app.extensions import db
from app.models import SocialAccount, SocialPost, SocialPostTarget
from app.social.queue import worker


def _story(session, *, status="published", style="post_link", linked=True):
    """A story target, optionally linked to a published feed target."""
    account = SocialAccount(
        platform="fake", external_id="EXT-story", display_name="Story Page",
        account_type="page", status="active",
    )
    session.add(account)
    session.flush()

    post = SocialPost(title="story post", status="published")
    session.add(post)
    session.flush()

    feed = SocialPostTarget(
        social_post_id=post.id, social_account_id=account.id,
        platform="fake", post_type="image", status="published",
        external_post_id="EXT-feed",
        permalink="https://example.invalid/p/abc123/",
    )
    session.add(feed)
    session.flush()

    story = SocialPostTarget(
        social_post_id=post.id, social_account_id=account.id,
        platform="fake", post_type="story", status=status,
        story_style=style,
        story_link_target_id=feed.id if linked else None,
    )
    session.add(story)
    session.flush()
    return post, feed, story


# --------------------------------------------------------------------------
# The model's own reading of "still needs a human"
# --------------------------------------------------------------------------

def test_a_published_linked_story_needs_the_sticker(session):
    _, _, story = _story(session)
    assert story.links_to_post is True
    assert story.needs_story_link is True


def test_a_plain_story_never_needs_the_sticker(session):
    _, _, story = _story(session, style="plain")
    assert story.links_to_post is False
    assert story.needs_story_link is False


def test_nothing_is_owed_before_the_story_publishes(session):
    """The story isn't live yet, so there is nothing to go and sticker."""
    _, _, story = _story(session, status="scheduled")
    assert story.links_to_post is True
    assert story.needs_story_link is False


def test_marking_it_done_settles_the_debt(session):
    _, _, story = _story(session)
    story.story_link_done_at = datetime.utcnow()
    session.flush()
    assert story.needs_story_link is False


def test_the_link_handed_over_is_the_posts_permalink(session):
    _, feed, story = _story(session)
    assert story.story_link_url == feed.permalink


def test_an_unlinked_story_offers_no_url(session):
    _, _, story = _story(session, linked=False)
    assert story.story_link_url is None


def test_only_stories_can_link_to_a_post(session):
    """story_style on a feed post is meaningless - guard against a stray
    value making a normal post claim a follow-up it can never need."""
    _, feed, _ = _story(session)
    feed.story_style = "post_link"
    session.flush()
    assert feed.links_to_post is False
    assert feed.needs_story_link is False


# --------------------------------------------------------------------------
# Publishing tells someone, because a story is gone in 24 hours
# --------------------------------------------------------------------------

def test_publishing_a_linked_story_notifies_its_creator(session, make_user):
    from app.models import Notification

    author = make_user("admin")
    _, _, story = _story(session)
    story.post.created_by_id = author.id
    session.flush()

    before = Notification.query.filter_by(user_id=author.id).count()
    worker._notify_story_link_pending(story)
    db.session.flush()

    after = Notification.query.filter_by(user_id=author.id).count()
    assert after == before + 1

    latest = (Notification.query.filter_by(user_id=author.id)
              .order_by(Notification.id.desc()).first())
    # The link is the whole point of the nudge - it must travel with it.
    assert story.story_link_url in latest.message


def test_a_plain_story_publishing_notifies_nobody(session, make_user):
    from app.models import Notification

    author = make_user("admin")
    _, _, story = _story(session, style="plain")
    story.post.created_by_id = author.id
    session.flush()

    before = Notification.query.filter_by(user_id=author.id).count()
    worker._notify_story_link_pending(story)
    db.session.flush()
    assert Notification.query.filter_by(user_id=author.id).count() == before


def test_a_notification_failure_never_fails_a_done_publish(
        session, make_user, monkeypatch):
    """The post is already live on the platform by this point - blowing up
    here would fail a publish that actually succeeded."""
    from app.social.services import audit

    author = make_user("admin")
    _, _, story = _story(session)
    story.post.created_by_id = author.id
    session.flush()

    def boom(*args, **kwargs):
        raise RuntimeError("notification backend down")

    monkeypatch.setattr(audit, "notify", boom)
    worker._notify_story_link_pending(story)  # must not raise


# --------------------------------------------------------------------------
# Closing the loop from the post page
# --------------------------------------------------------------------------

def test_marking_done_records_who_and_when(session, client, make_user, login):
    actor = make_user("admin", permissions=["manage_social"])
    _, _, story = _story(session)
    db.session.commit()

    login(actor)
    resp = client.post(f"/social/targets/{story.id}/story-link-done",
                       follow_redirects=True)
    assert resp.status_code == 200

    refreshed = db.session.get(SocialPostTarget, story.id)
    assert refreshed.story_link_done_at is not None
    assert refreshed.story_link_done_by_id == actor.id
    assert refreshed.needs_story_link is False


def test_a_plain_story_cannot_be_marked_done(session, client, make_user, login):
    actor = make_user("admin", permissions=["manage_social"])
    _, _, story = _story(session, style="plain")
    db.session.commit()

    login(actor)
    resp = client.post(f"/social/targets/{story.id}/story-link-done",
                       follow_redirects=True)
    assert "wasn&#39;t set to open a post" in resp.get_data(as_text=True) \
        or "wasn't set to open a post" in resp.get_data(as_text=True)

    refreshed = db.session.get(SocialPostTarget, story.id)
    assert refreshed.story_link_done_at is None


def test_reopening_puts_the_follow_up_back(session, client, make_user, login):
    actor = make_user("admin", permissions=["manage_social"])
    _, _, story = _story(session)
    story.story_link_done_at = datetime.utcnow()
    story.story_link_done_by_id = actor.id
    db.session.commit()

    login(actor)
    client.post(f"/social/targets/{story.id}/story-link-undo",
                follow_redirects=True)

    refreshed = db.session.get(SocialPostTarget, story.id)
    assert refreshed.story_link_done_at is None
    assert refreshed.needs_story_link is True


def test_the_post_page_shows_the_link_to_copy(
        session, client, make_user, login):
    actor = make_user("admin", permissions=["manage_social"])
    post, feed, story = _story(session)
    db.session.commit()

    login(actor)
    body = client.get(f"/social/posts/{post.id}").get_data(as_text=True)

    assert "Story sticker" in body
    assert feed.permalink in body
    assert "Mark done" in body
