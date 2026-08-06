"""Three things the Engage inbox got wrong about ad posts and comment threads.

1. An ad post is materialised with the placeholder title "Ad post" and no
   caption, so the AI drafting a reply was handed the two words "Ad post" as
   its entire idea of what the post said. Ad comments therefore got generic
   replies while Studio posts got specific ones.
2. The conversation pane showed that same placeholder and nothing else - no
   picture, no caption - so a person answering could not see what they were
   answering about either.
3. Auto-reply treated a reply inside a comment thread exactly like a comment
   on the post, so the bot answered into threads (often onto its own reply).
"""
import pytest

from app.models import SocialComment, SocialPost, SocialPostTarget
from app.social.services import engage


def _ad_target(session, make_target, caption=None, thumb=None):
    """A target standing in for a discovered ad post: placeholder title, and
    whatever caption/picture the sync managed to read."""
    _acct, post, target = make_target()
    post.title = "Ad post"
    post.source = "ad"
    target.status = "published"
    target.external_post_id = "AD_1"
    target.caption = caption
    target.thumbnail_url = thumb
    session.commit()
    return post, target


def _comment(session, target, ext="c1", parent=None, msg="Love this!"):
    c = SocialComment(target_id=target.id, platform=target.platform,
                      external_id=ext, parent_external_id=parent,
                      author_name="Sam", message=msg, is_ours=False,
                      status="open")
    session.add(c)
    session.commit()
    return c


# ======================================================================
# 1. The AI gets the post's caption, never the placeholder
# ======================================================================

def test_placeholder_title_is_not_sent_as_context(session, make_target):
    _post, target = _ad_target(session, make_target)
    c = _comment(session, target)
    # No caption yet -> nothing worth telling the model, rather than "Ad post".
    assert engage.post_context_for(c) is None


def test_the_ad_caption_becomes_the_context(session, make_target):
    _post, target = _ad_target(
        session, make_target, caption="Diwali offer — 20% off consultations")
    c = _comment(session, target)
    ctx = engage.post_context_for(c)
    assert "Diwali offer" in ctx
    assert "Ad post" not in ctx


def test_a_studio_post_still_sends_title_and_caption(session, make_target):
    _acct, post, target = make_target()
    post.title = "Hope+ IVF launch"
    target.caption = "Book your first consultation today"
    session.commit()
    ctx = engage.post_context_for(_comment(session, target))
    assert "Hope+ IVF launch" in ctx and "Book your first" in ctx


def test_context_is_none_when_there_is_no_target(session, make_target):
    """A comment with no target must not raise - it just has no context."""
    c = SocialComment(target_id=None, platform="fake", external_id="orphan",
                      message="hi")
    assert engage.post_context_for(c) is None


# ======================================================================
# 2. The conversation pane shows the real post
# ======================================================================

def test_the_preview_shows_the_caption_and_picture(session, client, make_user,
                                                   login, make_target):
    login(make_user("admin", permissions=["manage_social"]))
    _post, target = _ad_target(
        session, make_target, caption="Diwali offer CAPTIONXYZ",
        thumb="https://cdn.example/ad.jpg")
    c = _comment(session, target)

    # source=ad: the inbox has Post/Ad lanes and defaults to Post.
    body = client.get(f"/social/engage?source=ad&c={c.id}").get_data(as_text=True)
    assert "Diwali offer CAPTIONXYZ" in body
    assert "https://cdn.example/ad.jpg" in body
    # The picture must self-remove if the CDN link has expired.
    assert "onerror=\"this.remove()\"" in body


def test_the_preview_survives_a_post_with_no_picture(session, client,
                                                     make_user, login,
                                                     make_target):
    """No thumbnail is the normal case for a Studio post - the pane must fall
    back to the platform chip, not render a broken image."""
    login(make_user("admin", permissions=["manage_social"]))
    _post, target = _ad_target(session, make_target, caption="text only")
    c = _comment(session, target)
    # source=ad: the inbox has Post/Ad lanes and defaults to Post.
    body = client.get(f"/social/engage?source=ad&c={c.id}").get_data(as_text=True)
    assert "engage-context-thumb" not in body
    assert "text only" in body


# ======================================================================
# 3. Auto-reply answers comments, not replies inside a thread
# ======================================================================

def _cfg(**over):
    cfg = {"enabled": True, "max_len": 120, "max_per_post": 5,
           "blocklist": ["refund"], "answer_questions": False}
    cfg.update(over)
    return cfg


def test_a_reply_in_a_thread_is_never_auto_replied(session, make_target):
    from app.models import Client
    from tests.conftest import PYTEST_EMAIL_PREFIX

    cl = Client(client_name=f"{PYTEST_EMAIL_PREFIX}thread", status="active",
                comment_autoreply=True)
    session.add(cl)
    session.flush()
    _acct, post, target = make_target()
    post.client_id = cl.id
    target.external_post_id = "P1"
    session.commit()

    top = _comment(session, target, ext="top1")
    nested = _comment(session, target, ext="rep1", parent="top1")

    assert engage.comment_is_auto_safe(top, _cfg()) is True
    assert engage.comment_is_auto_safe(nested, _cfg()) is False


def test_a_parent_equal_to_the_post_id_is_still_a_top_level_comment(
        session, make_target):
    """Defensive: Facebook omits `parent` on a top-level comment today. If it
    ever returned the POST id there, a bare "has a parent" test would stop
    every auto-reply at once - so the check compares against the post id."""
    _post, target = _ad_target(session, make_target)
    c = _comment(session, target, ext="c9", parent=target.external_post_id)
    assert engage._is_reply_to_a_comment(c) is False


def test_a_reply_still_appears_in_the_inbox_for_a_human(session, client,
                                                        make_user, login,
                                                        make_target):
    """Exempt from the BOT, not hidden from the person - somebody still has to
    answer it."""
    login(make_user("admin", permissions=["manage_social"]))
    _post, target = _ad_target(session, make_target)
    _comment(session, target, ext="rep2", parent="top9",
             msg="NestedCommenterXYZ asks something")
    body = client.get("/social/engage?source=ad").get_data(as_text=True)
    assert "NestedCommenterXYZ" in body


# ======================================================================
# The provider read: Facebook and Instagram name the same three things
# differently, and asking one for the other's fields errors the whole call.
# ======================================================================

def test_facebook_post_details_are_mapped():
    from app.social.providers.meta_facebook import MetaFacebookProvider
    mapped = MetaFacebookProvider._map_post_details({
        "message": "Diwali offer",
        "full_picture": "https://cdn/fb.jpg",
        "permalink_url": "https://facebook.com/p/1",
    })
    assert mapped == {"caption": "Diwali offer",
                      "thumbnail_url": "https://cdn/fb.jpg",
                      "permalink": "https://facebook.com/p/1"}


def test_instagram_post_details_are_mapped():
    from app.social.providers.meta_instagram import MetaInstagramProvider
    mapped = MetaInstagramProvider._map_post_details({
        "caption": "Diwali offer",
        "media_url": "https://cdn/ig.jpg",
        "permalink": "https://instagram.com/p/1",
    })
    assert mapped["caption"] == "Diwali offer"
    assert mapped["thumbnail_url"] == "https://cdn/ig.jpg"


def test_instagram_video_prefers_the_thumbnail_over_the_file():
    """media_url on a VIDEO is the .mp4 itself - putting that in an <img> shows
    nothing. thumbnail_url exists only on video, so it wins when present."""
    from app.social.providers.meta_instagram import MetaInstagramProvider
    mapped = MetaInstagramProvider._map_post_details({
        "caption": "Reel",
        "media_url": "https://cdn/reel.mp4",
        "thumbnail_url": "https://cdn/reel.jpg",
        "media_type": "VIDEO",
    })
    assert mapped["thumbnail_url"] == "https://cdn/reel.jpg"


def test_a_failed_details_read_returns_empty_not_an_error(app, monkeypatch):
    """A preview is never worth failing a sync over."""
    from app.social.providers.meta_facebook import MetaFacebookProvider

    provider = MetaFacebookProvider()

    class _Graph:
        def get(self, *a, **k):
            raise RuntimeError("(#10) permission missing")

    monkeypatch.setattr(provider, "graph", lambda: _Graph())
    with app.app_context():
        assert provider.fetch_post_details("P1", "tok") == {}


def test_no_post_id_reads_nothing(app):
    from app.social.providers.meta_facebook import MetaFacebookProvider
    with app.app_context():
        assert MetaFacebookProvider().fetch_post_details("", "tok") == {}


# ======================================================================
# Backfill: an ad post discovered before this existed has no caption at all
# ======================================================================

def test_refresh_details_fills_a_bare_ad_target(session, make_target,
                                                monkeypatch):
    from app.social.services import engage_ads

    _acct, post, target = make_target()
    target.external_post_id = "AD_9"
    target.caption = None
    session.commit()

    class _P:
        @staticmethod
        def fetch_post_details(ext, token):
            return {"caption": "the real ad copy",
                    "thumbnail_url": "https://cdn/x.jpg",
                    "permalink": "https://fb/p/9"}

    monkeypatch.setattr(engage_ads, "get_provider", lambda p: _P)
    monkeypatch.setattr(
        "app.social.services.accounts.AccountManager.access_token",
        staticmethod(lambda acct: "tok"))

    engage_ads._refresh_details(target, target.account)
    assert target.caption == "the real ad copy"
    assert target.thumbnail_url == "https://cdn/x.jpg"
    assert target.permalink == "https://fb/p/9"


def test_refresh_details_never_blanks_what_we_already_had(session, make_target,
                                                          monkeypatch):
    """A Meta hiccup returning nothing must not wipe a caption we hold."""
    from app.social.services import engage_ads

    _acct, _post, target = make_target()
    target.caption = "kept"
    target.thumbnail_url = "https://cdn/old.jpg"
    session.commit()

    class _P:
        @staticmethod
        def fetch_post_details(ext, token):
            return {}

    monkeypatch.setattr(engage_ads, "get_provider", lambda p: _P)
    monkeypatch.setattr(
        "app.social.services.accounts.AccountManager.access_token",
        staticmethod(lambda acct: "tok"))

    engage_ads._refresh_details(target, target.account)
    assert target.caption == "kept"
    assert target.thumbnail_url == "https://cdn/old.jpg"


def test_refresh_details_keeps_a_studio_edited_caption(session, make_target,
                                                       monkeypatch):
    """A boosted STUDIO post that surfaces in the ads endpoint must KEEP the
    caption a human edited in Studio - the platform copy never overwrites an
    existing non-ad caption (only the thumbnail, which expires, is refreshed).
    """
    from app.social.services import engage_ads

    _acct, _post, target = make_target()          # post.source defaults to studio
    target.external_post_id = "BOOSTED_1"
    target.caption = "human-edited Studio caption"
    target.thumbnail_url = "https://cdn/old.jpg"
    session.commit()

    class _P:
        @staticmethod
        def fetch_post_details(ext, token):
            return {"caption": "platform copy",
                    "thumbnail_url": "https://cdn/new.jpg",
                    "permalink": "https://fb/p/1"}

    monkeypatch.setattr(engage_ads, "get_provider", lambda p: _P)
    monkeypatch.setattr(
        "app.social.services.accounts.AccountManager.access_token",
        staticmethod(lambda acct: "tok"))

    engage_ads._refresh_details(target, target.account)
    assert target.caption == "human-edited Studio caption"   # NOT overwritten
    assert target.thumbnail_url == "https://cdn/new.jpg"     # thumbnail refreshed


def test_refresh_details_skips_an_over_length_thumbnail(session, make_target,
                                                        monkeypatch):
    """Meta's signed CDN URLs can exceed the column limit; an over-length one is
    skipped (degrades to no picture) rather than raising on commit and dropping
    the whole discovery batch."""
    from app.social.services import engage_ads

    _acct, post, target = make_target()
    post.source = "ad"
    target.thumbnail_url = None
    target.permalink = None
    session.commit()

    class _P:
        @staticmethod
        def fetch_post_details(ext, token):
            return {"caption": "c",
                    "thumbnail_url": "https://cdn/" + "a" * 1200,   # > 1000
                    "permalink": "https://fb/" + "b" * 600}         # > 500

    monkeypatch.setattr(engage_ads, "get_provider", lambda p: _P)
    monkeypatch.setattr(
        "app.social.services.accounts.AccountManager.access_token",
        staticmethod(lambda acct: "tok"))

    engage_ads._refresh_details(target, target.account)
    assert target.thumbnail_url is None      # over-length skipped, no crash
    assert target.permalink is None
