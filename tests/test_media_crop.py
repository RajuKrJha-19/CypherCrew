"""Server-side image crop: the PIL util (deterministic pixel work) and the
composer route (gating, IDOR, validation). Storage is monkeypatched so nothing
touches R2 or the network.
"""
import io

import pytest
from PIL import Image

from app.social.media import crop as media_crop


def _png(w, h, color=(120, 60, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _dims(data):
    with Image.open(io.BytesIO(data)) as im:
        return im.size


# -- PIL util ---------------------------------------------------------------

def test_crop_produces_requested_pixel_box():
    out, mime = media_crop.crop_image(_png(1000, 1000), x=0, y=0, w=0.8, h=1.0)
    assert mime == "image/jpeg"
    w, h = _dims(out)
    assert (w, h) == (800, 1000)                 # 0.8×1000 wide, full height
    assert abs((w / h) - 0.8) < 0.01             # 4:5


def test_crop_downscales_long_edge_but_keeps_aspect():
    out, _ = media_crop.crop_image(_png(4000, 4000), x=0, y=0, w=1, h=1)
    w, h = _dims(out)
    assert max(w, h) <= 1920                      # capped
    assert abs((w / h) - 1.0) < 0.01             # square preserved


def test_crop_never_upscales_a_small_source():
    out, _ = media_crop.crop_image(_png(120, 90), x=0, y=0, w=1, h=1)
    assert _dims(out) == (120, 90)


def test_degenerate_rect_is_floored_not_crashed():
    out, _ = media_crop.crop_image(_png(500, 500), x=0.5, y=0.5, w=0, h=0)
    w, h = _dims(out)
    assert w >= 1 and h >= 1                      # min fraction applied, valid image


def test_out_of_bounds_rect_is_clamped_into_the_image():
    # x+w > 1 must not read past the edge.
    out, _ = media_crop.crop_image(_png(400, 400), x=0.9, y=0.9, w=0.5, h=0.5)
    w, h = _dims(out)
    assert w <= 400 and h <= 400 and w >= 1 and h >= 1


def test_non_image_bytes_raise_croperror():
    with pytest.raises(media_crop.CropError):
        media_crop.crop_image(b"not an image", x=0, y=0, w=1, h=1)


# -- route ------------------------------------------------------------------

def _stub_storage(monkeypatch, png=None):
    png = png or _png(600, 600)
    monkeypatch.setattr(
        "app.storage.storage_service.StorageService.read_bytes",
        lambda self, key: png)
    monkeypatch.setattr(
        "app.storage.storage_service.StorageService.put_bytes",
        lambda self, *, data, object_key, content_type=None: object_key)
    monkeypatch.setattr("app.social.media.pipeline.presigned_url",
                        lambda key: "http://example/preview")


def test_crop_route_happy_path(client, login, make_user, monkeypatch):
    _stub_storage(monkeypatch)
    login(make_user("employee", permissions=["manage_social"]))
    r = client.post("/social/api/media/crop", data={
        "object_key": "social_uploads/abc_photo.jpg",
        "x": "0.1", "y": "0.0", "w": "0.8", "h": "1.0"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["object_key"].startswith("social_uploads/")
    assert data["object_key"].endswith("_crop.jpg")
    assert data["mime"] == "image/jpeg" and data["is_image"] is True


def test_crop_route_forbidden_without_permission(client, login, make_user):
    login(make_user("employee"))                 # no manage_social
    r = client.post("/social/api/media/crop", data={
        "object_key": "social_uploads/x.jpg", "x": 0, "y": 0, "w": 1, "h": 1})
    assert r.status_code == 403


def test_crop_route_idor_blocks_unviewable_key(client, login, make_user):
    # An image key that is neither an ephemeral upload nor a viewable
    # task-file / client-asset -> denied before any bytes are read.
    login(make_user("employee", permissions=["manage_social"]))
    r = client.post("/social/api/media/crop", data={
        "object_key": "clients/acme/TASK-9/secret.jpg",
        "x": 0, "y": 0, "w": 1, "h": 1})
    assert r.status_code == 403


def test_crop_route_rejects_non_image_key(client, login, make_user):
    login(make_user("employee", permissions=["manage_social"]))
    r = client.post("/social/api/media/crop", data={
        "object_key": "social_uploads/clip.mp4", "x": 0, "y": 0, "w": 1, "h": 1})
    assert r.status_code == 400


def test_crop_route_rejects_bad_coords(client, login, make_user, monkeypatch):
    _stub_storage(monkeypatch)
    login(make_user("employee", permissions=["manage_social"]))
    r = client.post("/social/api/media/crop", data={
        "object_key": "social_uploads/abc_photo.jpg",
        "x": "nope", "y": 0, "w": 1, "h": 1})
    assert r.status_code == 400


def test_crop_route_missing_key(client, login, make_user):
    login(make_user("employee", permissions=["manage_social"]))
    r = client.post("/social/api/media/crop", data={"x": 0, "y": 0, "w": 1, "h": 1})
    assert r.status_code == 400
