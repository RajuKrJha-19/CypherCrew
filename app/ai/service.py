"""The façade the routes call. Gathers context from the DB + object storage,
invokes the resolved provider, and returns plain dicts/strings.

Media is read as bytes and size-capped here so a huge asset never inflates an
AI request; unreadable or oversized media is skipped, and generation still
proceeds on the brief + brand text alone.
"""
import mimetypes

from flask import current_app

from app.ai.base import CaptionContext, MediaInput
from app.ai.registry import get_provider

# Vision inputs we send for captions/alt-text. Video is intentionally excluded
# (heavy + not needed to caption from the brief); PDFs are for Phase-2 QA.
_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# Fallback caption limits when the social provider registry isn't reachable
# (e.g. the engine is off). The real values come from provider Capabilities.
_FALLBACK_LIMITS = {
    "instagram": 2200, "facebook": 63206, "youtube": 5000,
    "google_business": 1500, "linkedin": 3000, "twitter": 280, "x": 280,
}


def _mime_for(object_key):
    return (mimetypes.guess_type(object_key)[0] or "").lower()


def caption_limits(platforms):
    """Per-platform max caption chars, sourced from provider Capabilities."""
    limits = {}
    try:
        from app.social.registry import get_provider as social_provider
    except Exception:  # noqa: BLE001
        social_provider = None
    for p in platforms or []:
        cap = None
        if social_provider is not None:
            prov = social_provider(p)
            cap = getattr(getattr(prov, "capabilities", None),
                          "max_caption_chars", None)
        limits[p] = cap or _FALLBACK_LIMITS.get(p)
    return {k: v for k, v in limits.items() if v}


def _load_media(items):
    """items: iterable of (object_key, label). Returns MediaInput list, images
    only, each within the size cap; every failure is skipped silently
    (best-effort context - media never turns a generation into a hard error)."""
    wanted = [(k, l) for (k, l) in items if k and _mime_for(k) in _IMAGE_MIMES]
    if not wanted:
        return []

    max_bytes = int(current_app.config.get("AI_MEDIA_MAX_MB", 10)) * 1024 * 1024
    try:
        from app.storage.storage_service import StorageService
        storage = StorageService()
    except Exception:  # noqa: BLE001 - no storage -> generate on brief alone
        return []

    out = []
    for object_key, label in wanted:
        try:
            data = storage.read_bytes(object_key)
        except Exception:  # noqa: BLE001
            continue
        if not data or len(data) > max_bytes:
            continue
        out.append(MediaInput(data=data, mime_type=_mime_for(object_key),
                             label=label))
    return out


# -- public API -------------------------------------------------------------

def generate_caption(*, brief="", industry=None, brand_voice=None,
                     brand_notes=None, platforms=None, media=None):
    """media: iterable of (object_key, label). Returns a plain dict."""
    provider = get_provider()
    ctx = CaptionContext(
        brief=brief or "",
        industry=industry,
        brand_voice=brand_voice,
        brand_notes=brand_notes,
        platforms=list(platforms or []),
        caption_limits=caption_limits(platforms or []),
        media=_load_media(media or []),
    )
    result = provider.generate_caption(ctx)
    return {
        "caption": result.caption,
        "per_platform": result.per_platform,
        "hashtags": result.hashtags,
        "first_comment": result.first_comment,
    }


def generate_alt_text(object_key, label=None):
    """One alt-text line for a single image object, or "" if it can't be read."""
    provider = get_provider()
    images = _load_media([(object_key, label)])
    if not images:
        return ""
    return provider.generate_alt_text(images[0])
