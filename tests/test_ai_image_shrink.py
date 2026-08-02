"""Downscaling images sent to the AI vision call - only oversized ones, and
only the throwaway copy (the stored original is never touched here). Best-effort
so it never breaks generation.
"""
from io import BytesIO

import pytest

from app.ai import service

PIL = pytest.importorskip("PIL")           # skip if Pillow isn't installed
from PIL import Image                        # noqa: E402


def _png_bytes(w, h):
    buf = BytesIO()
    Image.new("RGB", (w, h), (120, 60, 200)).save(buf, format="PNG")
    return buf.getvalue()


def test_large_image_is_downscaled(app):
    big = _png_bytes(4000, 3000)
    with app.app_context():
        out, mime = service._shrink_image(big, "image/png")
    assert mime == "image/jpeg"                       # re-encoded
    assert len(out) < len(big)                        # smaller payload
    w, h = Image.open(BytesIO(out)).size
    assert max(w, h) <= app.config["AI_IMAGE_MAX_DIM"]  # capped
    assert (w, h) == (1568, 1176)                     # aspect ratio preserved


def test_small_image_is_untouched(app):
    small = _png_bytes(1080, 1080)
    with app.app_context():
        out, mime = service._shrink_image(small, "image/png")
    assert out is small and mime == "image/png"       # passthrough, no re-encode


def test_non_image_is_untouched(app):
    with app.app_context():
        out, mime = service._shrink_image(b"%PDF-1.4 ...", "application/pdf")
    assert out == b"%PDF-1.4 ..." and mime == "application/pdf"


def test_bad_bytes_return_original(app):
    with app.app_context():
        out, mime = service._shrink_image(b"not an image", "image/png")
    assert out == b"not an image" and mime == "image/png"   # best-effort
