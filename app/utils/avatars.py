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
