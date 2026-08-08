"""Instagram co-author (Collab) support: when the composer puts collaborator
usernames on the content, the media container invites them onto the SAME post
(Feed + Reels). Stories can't have collaborators, and the feature is inert when
no usernames are present. Graph is faked; nothing hits Meta.
"""
import json
from types import SimpleNamespace

from app.social.dto import PostContent
from app.social.providers.meta_instagram import MetaInstagramProvider


class _Graph:
    def __init__(self):
        self.posts = []

    def post(self, path, token=None, data=None):
        self.posts.append((str(path), dict(data or {})))
        return {"id": "CONT_" + str(len(self.posts))}


def _ig(monkeypatch, graph):
    ig = MetaInstagramProvider()
    monkeypatch.setattr(ig, "graph", lambda: graph)
    monkeypatch.setattr(ig, "_media_url", lambda m: "https://cdn/x")
    monkeypatch.setattr(ig, "_full_caption", lambda c: c.caption)
    return ig


def _content(post_type, collaborators=None):
    extra = {} if collaborators is None else {"collaborators": collaborators}
    return PostContent(
        platform="instagram", post_type=post_type, caption="hi",
        media=[SimpleNamespace(mime_type="image/jpeg")], extra=extra)


def _target():
    return SimpleNamespace(account=SimpleNamespace(external_id="IG_1"))


def test_ig_image_invites_collaborators_cleaned(monkeypatch):
    g = _Graph()
    _ig(monkeypatch, g).start_publish(
        _target(), _content("image", ["dr_sunil", "@other", "  "]), "tok")
    _, data = g.posts[-1]                     # the feed container
    assert json.loads(data["collaborators"]) == ["dr_sunil", "other"]


def test_ig_reel_invites_collaborators(monkeypatch):
    g = _Graph()
    content = _content("reel", ["dr_sunil"])
    content.media = [SimpleNamespace(mime_type="video/mp4")]
    _ig(monkeypatch, g).start_publish(_target(), content, "tok")
    _, data = g.posts[-1]
    assert json.loads(data["collaborators"]) == ["dr_sunil"]


def test_ig_carousel_invites_collaborators_on_the_parent(monkeypatch):
    g = _Graph()
    content = _content("carousel", ["dr_sunil"])
    content.media = [SimpleNamespace(mime_type="image/jpeg"),
                     SimpleNamespace(mime_type="image/jpeg")]
    _ig(monkeypatch, g).start_publish(_target(), content, "tok")
    _, parent = g.posts[-1]                   # the CAROUSEL parent, not a child
    assert parent.get("media_type") == "CAROUSEL"
    assert json.loads(parent["collaborators"]) == ["dr_sunil"]


def test_ig_story_never_gets_collaborators(monkeypatch):
    g = _Graph()
    content = _content("story", ["dr_sunil"])
    content.media = [SimpleNamespace(mime_type="image/jpeg")]
    _ig(monkeypatch, g).start_publish(_target(), content, "tok")
    _, data = g.posts[-1]
    assert "collaborators" not in data


def test_ig_no_collaborators_key_when_absent(monkeypatch):
    g = _Graph()
    _ig(monkeypatch, g).start_publish(_target(), _content("image", None), "tok")
    _, data = g.posts[-1]
    assert "collaborators" not in data
