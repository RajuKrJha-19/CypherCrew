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


def file_badge(file):
    """A small format badge for a file tile - {label, cls} - or None.

    Only for formats worth flagging at a glance (design/document files);
    plain photos and videos don't get one. Matched on extension first (the
    reliable signal) then mime, since browsers mislabel these often.
    """
    if file is None:
        return None

    mime = (getattr(file, "mime_type", "") or "").lower()
    name = (getattr(file, "original_filename", "") or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""

    def badge(label, cls):
        return {"label": label, "cls": cls}

    if ext == "psd" or "photoshop" in mime:
        return badge("PS", "ps")
    if ext == "ai" or "illustrator" in mime:
        return badge("AI", "ai")
    if ext == "pdf" or mime == "application/pdf":
        return badge("PDF", "pdf")
    if ext == "eps" or "postscript" in mime:
        return badge("EPS", "eps")
    if ext == "indd":
        return badge("ID", "indd")
    if ext == "xd":
        return badge("XD", "xd")
    if ext in ("fig", "figma"):
        return badge("Fig", "fig")
    if ext == "sketch":
        return badge("Sk", "sketch")

    return None
