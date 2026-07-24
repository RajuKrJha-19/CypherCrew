"""Profile-picture helpers.

Avatars live in R2 like every other upload, so the browser can't hit the
object directly - it needs a short-lived presigned URL, minted at render
time. `avatar_url` returns that URL, or None so templates fall back to the
user's initials. It never raises: a storage hiccup should show initials,
not break the page.
"""

from app.storage.storage_service import StorageService


def avatar_url(user):
    """Presigned preview URL for the user's avatar, or None."""
    key = getattr(user, "avatar_key", None) if user is not None else None

    if not key:
        return None

    try:
        return StorageService().preview_url(object_key=key)
    except Exception:
        return None


def file_preview_url(file):
    """Direct presigned preview URL for a task file's object, or None.

    Used for <video> frame thumbnails: pointing the element straight at R2
    (rather than the redirecting /preview route) makes metadata + range
    seeks behave. Longer expiry so a lazily-loaded tile still resolves.
    """
    key = getattr(file, "object_key", None) if file is not None else None

    if not key:
        return None

    try:
        return StorageService().preview_url(object_key=key, expires_in=3600)
    except Exception:
        return None


def file_thumbnail_url(file):
    """Direct presigned URL for a file's *generated* thumbnail, or None.

    The grid points <img> straight at this so the browser fetches the
    small cached webp from R2 itself - no redirect hop through the app,
    no per-tile Flask request, and nothing generated during render. A
    page of 69 tiles was paying for 69 round-trips through a 302 route;
    this collapses each to a single direct fetch the browser can run in
    parallel.

    Returns a URL only when a thumbnail actually exists (state ready +
    key). Not-ready files return None and the template shows a light
    placeholder instead of proxying the full-size original.
    """
    if file is None:
        return None

    if getattr(file, "thumbnail_state", None) != "ready":
        return None

    key = getattr(file, "thumbnail_key", None)

    if not key:
        return None

    try:
        return StorageService().preview_url(object_key=key, expires_in=3600)
    except Exception:
        return None


# Extension -> (badge label, css class). Every file gets a badge now, so
# the format is obvious at a glance in the grid. Labels are kept to <=4
# chars so the chip stays small. Design/document formats get their own
# brand-ish colour; everything else is grouped by category (img / video /
# audio / doc / sheet / slide / archive).
_BADGE_BY_EXT = {
    # Adobe / design
    "psd": ("PSD", "ps"),
    "ai": ("AI", "ai"),
    "eps": ("EPS", "eps"),
    "indd": ("INDD", "indd"),
    "xd": ("XD", "xd"),
    "fig": ("FIG", "fig"),
    "figma": ("FIG", "fig"),
    "sketch": ("SK", "sketch"),
    # documents
    "pdf": ("PDF", "pdf"),
    "doc": ("DOC", "doc"),
    "docx": ("DOC", "doc"),
    "rtf": ("RTF", "doc"),
    "txt": ("TXT", "doc"),
    "xls": ("XLS", "sheet"),
    "xlsx": ("XLS", "sheet"),
    "csv": ("CSV", "sheet"),
    "ppt": ("PPT", "slide"),
    "pptx": ("PPT", "slide"),
    # images
    "jpg": ("JPG", "img"),
    "jpeg": ("JPG", "img"),
    "png": ("PNG", "img"),
    "gif": ("GIF", "img"),
    "webp": ("WEBP", "img"),
    "svg": ("SVG", "img"),
    "avif": ("AVIF", "img"),
    "heic": ("HEIC", "img"),
    "bmp": ("BMP", "img"),
    "tiff": ("TIFF", "img"),
    "tif": ("TIFF", "img"),
    # video
    "mp4": ("MP4", "video"),
    "mov": ("MOV", "video"),
    "webm": ("WEBM", "video"),
    "mkv": ("MKV", "video"),
    "avi": ("AVI", "video"),
    "m4v": ("M4V", "video"),
    # audio
    "mp3": ("MP3", "audio"),
    "wav": ("WAV", "audio"),
    "aac": ("AAC", "audio"),
    "ogg": ("OGG", "audio"),
    "m4a": ("M4A", "audio"),
    "flac": ("FLAC", "audio"),
    # archives
    "zip": ("ZIP", "archive"),
    "rar": ("RAR", "archive"),
    "7z": ("7Z", "archive"),
    "tar": ("TAR", "archive"),
    "gz": ("GZ", "archive"),
}


def file_badge(file):
    """A small format badge for a file tile - {label, cls}.

    Returns a badge for every file (never None), matched on the filename
    extension first (the reliable signal) then mime, since browsers
    mislabel design formats often.
    """
    if file is None:
        return None

    mime = (getattr(file, "mime_type", "") or "").lower()
    name = (getattr(file, "original_filename", "") or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""

    known = _BADGE_BY_EXT.get(ext)

    if known:
        return {"label": known[0], "cls": known[1]}

    # No/odd extension - lean on the mime type.
    if "photoshop" in mime:
        return {"label": "PSD", "cls": "ps"}
    if "illustrator" in mime:
        return {"label": "AI", "cls": "ai"}
    if mime == "application/pdf":
        return {"label": "PDF", "cls": "pdf"}
    if mime.startswith("image/"):
        return {"label": "IMG", "cls": "img"}
    if mime.startswith("video/"):
        return {"label": "VID", "cls": "video"}
    if mime.startswith("audio/"):
        return {"label": "AUD", "cls": "audio"}

    # Last resort - the raw extension (or FILE) in a neutral chip.
    return {"label": (ext[:4].upper() if ext else "FILE"), "cls": "default"}
