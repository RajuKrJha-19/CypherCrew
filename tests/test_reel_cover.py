"""Instagram Reel cover: custom image (cover_url) or a picked frame
(thumb_offset), wired composer -> post -> content.extra -> IG container."""
import json
from types import SimpleNamespace

from app.extensions import db
from app.models import SocialAccount, SocialPost, SocialPostTarget
from app.social.dto import MediaRef, PostContent
from app.social.providers.meta_instagram import MetaInstagramProvider
from app.social.services import publishing


class _Graph:
    def __init__(self):
        self.data = {}

    def post(self, path, token=None, data=None):
        self.data = dict(data or {})
        return {"id": "CONT_1"}


def _ig(monkeypatch):
    ig = MetaInstagramProvider()
    g = _Graph()
    monkeypatch.setattr(ig, "graph", lambda: g)
    monkeypatch.setattr(ig, "_media_url", lambda m: "http://vid")
    return ig, g


def _reel_content(extra):
    return PostContent(
        platform="instagram", post_type="reel",
        media=[MediaRef(object_key="v.mp4", mime_type="video/mp4")],
        extra=extra)


def test_ig_reel_uses_custom_cover_url(monkeypatch):
    ig, g = _ig(monkeypatch)
    ig.start_publish(SimpleNamespace(account=SimpleNamespace(external_id="IG")),
                     _reel_content({"reel_cover_url": "http://cover.jpg"}), "t")
    assert g.data.get("cover_url") == "http://cover.jpg"
    assert "thumb_offset" not in g.data


def test_ig_reel_uses_thumb_offset(monkeypatch):
    ig, g = _ig(monkeypatch)
    ig.start_publish(SimpleNamespace(account=SimpleNamespace(external_id="IG")),
                     _reel_content({"reel_thumb_offset": 3200}), "t")
    assert g.data.get("thumb_offset") == 3200
    assert "cover_url" not in g.data


def test_ig_reel_default_cover_when_none(monkeypatch):
    ig, g = _ig(monkeypatch)
    ig.start_publish(SimpleNamespace(account=SimpleNamespace(external_id="IG")),
                     _reel_content({}), "t")
    assert "cover_url" not in g.data and "thumb_offset" not in g.data


def test_build_content_carries_thumb_offset(session):
    a = SocialAccount(platform="instagram", external_id="BC1", display_name="ig",
                      account_type="ig_business", status="active")
    post = SocialPost(status="approved", title="bc", reel_thumb_offset=3200)
    db.session.add_all([a, post])
    db.session.flush()
    t = SocialPostTarget(social_post_id=post.id, social_account_id=a.id,
                         platform="instagram", post_type="reel", status="draft")
    db.session.add(t)
    db.session.commit()

    content = publishing.build_content(t)
    assert content.extra.get("reel_thumb_offset") == 3200


def test_composer_stores_reel_thumb_offset(session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    a = SocialAccount(platform="instagram", external_id="RC1", display_name="ig",
                      account_type="ig_business", status="active")
    db.session.add(a)
    db.session.commit()

    value = "social_uploads/r.mp4::video/mp4"
    client.post("/social/posts", data={
        "title": "rc", "post_type": "reel", "caption": "hi",
        "upload_media": value, "account_ids": [str(a.id)],
        "reel_cover_mode": "frame", "reel_thumb_offset": "3200",
        "media_measurements": json.dumps({
            f"upload_media|{value}": {"width": 1080, "height": 1920,
                                      "duration": 10}}),
    }, follow_redirects=True)

    post = SocialPost.query.filter_by(title="rc").first()
    assert post.reel_thumb_offset == 3200
    assert post.reel_cover_key is None
