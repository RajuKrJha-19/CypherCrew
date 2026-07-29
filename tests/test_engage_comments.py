"""Engage comment fetch: Facebook and Instagram have different schemas."""
from app.social.providers.meta_facebook import MetaFacebookProvider
from app.social.providers.meta_instagram import MetaInstagramProvider


class _Graph:
    def __init__(self, data):
        self._data = data
        self.fields = None

    def get(self, path, token=None, params=None):
        self.fields = (params or {}).get("fields", "")
        return {"data": self._data}


def test_instagram_maps_text_username_timestamp(monkeypatch):
    ig = MetaInstagramProvider()
    g = _Graph([{"id": "c1", "text": "Nice reel!", "username": "satpal",
                 "timestamp": "2026-07-29T11:00:00+0000"}])
    monkeypatch.setattr(ig, "graph", lambda: g)

    out = ig.list_comments("IGMEDIA_1", "tok")
    assert "text" in g.fields and "username" in g.fields   # IG's own fields
    c = out[0]
    assert c["message"] == "Nice reel!"          # text -> message (the bug)
    assert c["author_name"] == "satpal"          # username -> author_name
    assert c["created_time"] == "2026-07-29T11:00:00+0000"


def test_facebook_maps_from_name_and_picture(monkeypatch):
    fb = MetaFacebookProvider()
    g = _Graph([{"id": "c2", "message": "Congrats!",
                 "from": {"id": "u1", "name": "Manas",
                          "picture": {"data": {"url": "http://pic/1"}}},
                 "created_time": "2026-07-29T10:00:00+0000"}])
    monkeypatch.setattr(fb, "graph", lambda: g)

    out = fb.list_comments("PAGE_1", "tok")
    assert "from{id,name,picture}" in g.fields
    c = out[0]
    assert c["message"] == "Congrats!"
    assert c["author_name"] == "Manas"
    assert c["author_id"] == "u1"
    assert c["author_pic"] == "http://pic/1"


def test_emulator_shape_still_maps_via_fallbacks(monkeypatch):
    """The local emulator answers with the Facebook shape for both networks;
    the `or`-fallbacks must still populate message + name."""
    ig = MetaInstagramProvider()
    g = _Graph([{"id": "c3", "message": "hi", "from": {"name": "Neha"},
                 "created_time": "t"}])
    monkeypatch.setattr(ig, "graph", lambda: g)

    c = ig.list_comments("X", "tok")[0]
    assert c["message"] == "hi"
    assert c["author_name"] == "Neha"
