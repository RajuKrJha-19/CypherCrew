"""Server-side image crop for the composer's reframe tool.

The client picks a rectangle over the (cross-origin) preview; the pixels are
cut HERE so nothing depends on a client canvas reading a cross-origin R2 image
(which would taint the canvas / need a bucket CORS policy). Mirrors the PIL
read->transform->write pattern in app/services/thumbnails.py: decompression-bomb
guard, EXIF-orientation fix, RGB flatten, downscale-only, JPEG out.

Pure function of bytes -> bytes; storage + routing live in the caller.
"""
import io

# A crop smaller than this fraction of an edge is almost certainly a stray
# click, not an intended selection; we floor it so a degenerate rect can never
# ask for a 0-pixel (or, after rounding, upscaled) image.
_MIN_FRACTION = 0.02

# Same decompression-bomb ceiling the thumbnailer uses.
_MAX_PIXELS = 40_000_000


class CropError(Exception):
    """The submitted image could not be cropped (unreadable / not an image)."""


def _clamp01(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def crop_image(data, *, x, y, w, h, max_edge=1920):
    """Crop `data` (image bytes) to the normalised rectangle (fractions 0..1 of
    the EXIF-corrected source) and return (jpeg_bytes, "image/jpeg").

    Downscales the result so its long edge is <= max_edge (never upscales, so a
    small source keeps its resolution). The rectangle is clamped into the image
    and floored to a sane minimum, so no input can produce a 0-px or upscaled
    output. Raises CropError if the bytes are not a decodable image.
    """
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = _MAX_PIXELS

    # Normalise + clamp the rectangle so left/top stay in-bounds and there is
    # always a non-empty box to cut.
    x = _clamp01(x)
    y = _clamp01(y)
    w = max(_MIN_FRACTION, min(_clamp01(w), 1.0 - x))
    h = max(_MIN_FRACTION, min(_clamp01(h), 1.0 - y))

    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)      # honour phone orientation
            if img.mode != "RGB":
                img = img.convert("RGB")            # flatten alpha/P/CMYK -> JPEG-safe
            W, H = img.size

            left = int(round(x * W))
            top = int(round(y * H))
            right = int(round((x + w) * W))
            bottom = int(round((y + h) * H))
            # Guarantee at least a 1px box even on a tiny source.
            right = min(W, max(left + 1, right))
            bottom = min(H, max(top + 1, bottom))

            cropped = img.crop((left, top, right, bottom))
            if max(cropped.size) > max_edge:
                cropped.thumbnail((max_edge, max_edge), Image.LANCZOS)

            out = io.BytesIO()
            cropped.save(out, format="JPEG", quality=90)
            return out.getvalue(), "image/jpeg"
    except CropError:
        raise
    except Exception as exc:  # noqa: BLE001 - any decode failure -> clean typed error
        raise CropError(str(exc)) from exc
