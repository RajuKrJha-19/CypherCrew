"""Phase 1 composer + Facebook Stories.

Covers: FB Story capabilities + publish (photo sync, video async), the
capability-driven story generalization, per-channel first comment with the
supports_first_comment / no-story gates, and per-media alt-text capture.
"""
import json
from types import SimpleNamespace

from app.extensions import db
from app.models import SocialAccount, SocialPost
from app.social.dto import MediaRef, PostContent, StepStatus
from app.social.providers import meta_facebook
from app.social.providers.meta_facebook import MetaFacebookProvider
from app.social.providers.simulation import CAPABILITY_PROFILES


class _FakeGraph:
    """Records Graph calls and answers the story/reel endpoints."""
    def __init__(self):
        self.calls = []

    def post(self, path, token=None, data=None):
        self.calls.append((path, data or {}))
        if path.endswith("/photos"):
            return {"id": "PHOTO_1"}
        if path.endswith("/photo_stories"):
            return {"post_id": "STORY_1"}
        if path.endswith("/video_stories"):
            if (data or {}).get("upload_phase") == "start":
                return {"video_id": "VS_1", "upload_url": "http://up/VS_1"}
            return {"success": True, "post_id": "PAGE_VS_1"}
        return {"id": "X"}

    def get(self, node, token=None, params=None):
        fields = (params or {}).get("fields", "")
        if "status" in fields:
            return {"status": {"video_status": "ready"}}
        if "permalink_url" in fields:
            return {"permalink_url": "/VS_1"}
        return {}


# -- capabilities -----------------------------------------------------------

def test_facebook_declares_story_and_sim_matches():
    caps = MetaFacebookProvider.capabilities
    assert "story" in caps.post_types
    assert caps.story_support is True
    assert caps.spec_for("story") is not None       # so validation runs
    sim = CAPABILITY_PROFILES["facebook"]
    assert "story" in sim.post_types and sim.story_support is True


# -- publish ----------------------------------------------------------------

def _fb_with_fake_graph(monkeypatch):
    fb = MetaFacebookProvider()
    fg = _FakeGraph()
    monkeypatch.setattr(fb, "graph", lambda: fg)
    monkeypatch.setattr(fb, "_media_url", lambda m: "http://media/file")
    return fb, fg


def _target():
    return SimpleNamespace(account=SimpleNamespace(external_id="PAGE1"))


def test_fb_photo_story_publishes_synchronously(monkeypatch):
    fb, fg = _fb_with_fake_graph(monkeypatch)
    content = PostContent(platform="facebook", post_type="story",
                          media=[MediaRef(object_key="p.jpg",
                                          mime_type="image/jpeg")])
    step = fb.start_publish(_target(), content, "tok")
    assert step.status == StepStatus.DONE.value
    assert step.external_post_id == "STORY_1"
    paths = [c[0] for c in fg.calls]
    # uploaded unpublished, then posted as a story - never on the feed.
    assert any(p.endswith("/photos") for p in paths)
    assert any(p.endswith("/photo_stories") for p in paths)
    assert not any(p.endswith("/feed") for p in paths)
    # published=false on the photo upload
    photo_call = next(c for c in fg.calls if c[0].endswith("/photos"))
    assert photo_call[1].get("published") == "false"


def test_fb_video_story_is_async_then_publishes(monkeypatch):
    fb, fg = _fb_with_fake_graph(monkeypatch)
    monkeypatch.setattr(meta_facebook, "hosted_reel_upload",
                        lambda *a, **k: None)
    content = PostContent(platform="facebook", post_type="story",
                          media=[MediaRef(object_key="v.mp4",
                                          mime_type="video/mp4")])
    step = fb.start_publish(_target(), content, "tok")
    assert step.status == StepStatus.PENDING.value
    assert step.provider_state["video_id"] == "VS_1"
    assert step.provider_state["kind"] == "story"

    done = fb.poll_publish(_target(), step.provider_state, "tok")
    assert done.status == StepStatus.DONE.value
    assert done.external_post_id == "VS_1"
    # A story carries no caption on either phase.
    for _path, data in fg.calls:
        assert "description" not in data


# -- per-channel first comment + gating -------------------------------------

def _acct(platform, cid=None):
    a = SocialAccount(platform=platform, external_id=f"P1-{platform}",
                      display_name=platform, account_type="page",
                      status="active", client_id=cid)
    db.session.add(a)
    db.session.flush()
    return a


def test_per_channel_first_comment_override_and_gates(
        session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    fb = _acct("facebook").id
    gbp = _acct("google_business").id      # supports_first_comment = False
    db.session.commit()

    value = "social_uploads/x.jpg::image/jpeg"
    resp = client.post("/social/posts", data={
        "title": "fc", "post_type": "image", "caption": "hi",
        "first_comment": "shared FC",
        "first_comment_facebook": "FB-only FC",
        "upload_media": value,
        "account_ids": [str(fb), str(gbp)],
    }, follow_redirects=True)
    assert resp.status_code == 200

    post = SocialPost.query.filter_by(title="fc").first()
    by = {t.platform: t for t in post.targets}
    # Facebook: its own override wins.
    assert by["facebook"].first_comment == "FB-only FC"
    # Google Business can't post comments -> never stored, even shared.
    assert by["google_business"].first_comment is None


def test_story_target_never_carries_a_first_comment(
        session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    fb = _acct("facebook").id
    db.session.commit()

    value = "social_uploads/s.jpg::image/jpeg"
    client.post("/social/posts", data={
        "title": "st", "post_type": "story", "caption": "",
        "first_comment": "shared", "upload_media": value,
        "account_ids": [str(fb)],
    }, follow_redirects=True)

    post = SocialPost.query.filter_by(title="st").first()
    assert post.targets[0].post_type == "story"
    assert post.targets[0].first_comment is None


# -- alt text ---------------------------------------------------------------

def test_alt_text_is_captured(session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    fb = _acct("facebook").id
    db.session.commit()

    value = "social_uploads/pic.jpg::image/jpeg"
    client.post("/social/posts", data={
        "title": "alt", "post_type": "image", "caption": "hi",
        "upload_media": value,
        "account_ids": [str(fb)],
        "media_alt": json.dumps({f"upload_media|{value}": "A red bicycle"}),
    }, follow_redirects=True)

    post = SocialPost.query.filter_by(title="alt").first()
    media = post.targets[0].media
    assert media and media[0].alt_text == "A red bicycle"
