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
