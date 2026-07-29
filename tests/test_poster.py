"""Video poster frames + the /media/poster endpoint."""
from app.social.media import poster, transcode


def test_poster_key_is_deterministic_and_prefixed():
    k1 = poster._poster_key("social_uploads/x.mp4")
    k2 = poster._poster_key("social_uploads/x.mp4")
    assert k1 == k2
    assert k1.startswith("social_uploads/posters/")
    assert k1.endswith(".jpg")
    assert poster._poster_key("a") != poster._poster_key("b")


def test_poster_url_is_none_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(transcode, "available", lambda: False)

    class _NoStore:
        def exists(self, *, object_key):
            return False
    monkeypatch.setattr(poster, "StorageService", lambda: _NoStore())

    assert poster.poster_url("social_uploads/missing.mp4") is None


def test_poster_url_serves_a_cached_poster(monkeypatch):
    # A poster that already exists is served without touching ffmpeg.
    class _Store:
        def exists(self, *, object_key):
            return True
    monkeypatch.setattr(poster, "StorageService", lambda: _Store())
    monkeypatch.setattr(poster, "presigned_url",
                        lambda key, **k: f"https://r2/{key}")

    url = poster.poster_url("social_uploads/clip.mp4")
    assert url.startswith("https://r2/social_uploads/posters/")


def test_media_poster_route_rejects_foreign_keys(
        session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    assert client.get(
        "/social/media/poster?key=etc/passwd").status_code == 404
    assert client.get(
        "/social/media/poster?key=../secret").status_code == 404
    assert client.get("/social/media/poster").status_code == 404
