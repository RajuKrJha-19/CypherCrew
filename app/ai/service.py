"""The façade the routes call. Gathers context from the DB + object storage,
invokes the resolved provider, and returns plain dicts/strings.

Media is read as bytes and size-capped here so a huge asset never inflates an
AI request; unreadable or oversized media is skipped, and generation still
proceeds on the brief + brand text alone.
"""
import mimetypes

from flask import current_app

from app.ai.base import (
    CaptionContext, Finding, MediaCheckContext, MediaInput,
)
from app.ai.registry import get_provider

# Vision inputs we send for captions/alt-text. Video is intentionally excluded
# (heavy + not needed to caption from the brief). Media QA also accepts PDFs
# (design deliverables + brand-guideline docs).
_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_QA_MIMES = _IMAGE_MIMES | {"application/pdf"}
# At most this many brand-guideline docs are fed to a QA call (cost bound).
_MAX_GUIDELINES = 2

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


def _read_bytes(object_key):
    """Raw bytes for an object within the size cap, or None. Best-effort:
    every failure (no storage, unreadable, oversized) returns None."""
    if not object_key:
        return None
    max_bytes = int(current_app.config.get("AI_MEDIA_MAX_MB", 10)) * 1024 * 1024
    try:
        from app.storage.storage_service import StorageService
        data = StorageService().read_bytes(object_key)
    except Exception:  # noqa: BLE001
        return None
    if not data or len(data) > max_bytes:
        return None
    return data


def _load_media(items, allowed=_IMAGE_MIMES):
    """items: iterable of (object_key, label). Returns MediaInput list of the
    allowed mime types, each within the size cap; every failure is skipped
    silently (best-effort context - media never turns a call into a hard
    error)."""
    wanted = [(k, l) for (k, l) in items if k and _mime_for(k) in allowed]
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
    provider = get_provider("caption")
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
    provider = get_provider("caption")
    images = _load_media([(object_key, label)])
    if not images:
        return ""
    return provider.generate_alt_text(images[0])


# -- Media QA (Phase 2) ------------------------------------------------------

_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}


def _worst(findings):
    return max((_SEVERITY_RANK.get(f.severity, 0) for f in findings), default=0)


def _persist_check(task_file, provider, created_by_id, findings):
    from app.extensions import db
    from app.models import AICheck

    status = "flagged" if _worst(findings) >= 1 else "clean"
    model_name = ("simulation" if provider.key == "simulation"
                  else (provider.model or provider.key))
    row = AICheck(
        task_file_id=task_file.id,
        status=status,
        model=model_name,
        findings=[f.as_dict() for f in findings],
        created_by_id=created_by_id,
    )
    db.session.add(row)
    db.session.commit()
    return {"check_id": row.id, "status": status, "model": model_name,
            "findings": row.findings}


def check_media(task_file, created_by_id=None):
    """Run an advisory QA pass on a submitted deliverable and persist an
    AICheck. `task_file` is a resolved TaskFile (the route owns existence +
    permission checks). Returns a plain dict."""
    from app.models import ClientAsset

    provider = get_provider("qa")
    task = getattr(task_file, "task", None)
    client = getattr(task, "client", None)

    mime = (task_file.mime_type
            or _mime_for(task_file.object_key or "")).lower()
    label = task_file.original_filename or "deliverable"

    # Only images + PDFs are reviewable. For anything else (video, unreadable),
    # record an advisory info row rather than a meaningless empty check.
    if mime not in _QA_MIMES:
        return _persist_check(task_file, provider, created_by_id, [Finding(
            severity="info", category="spec",
            message="Automated QA supports images and PDFs — review this file "
                    "manually.")])

    # The deliverable is loaded by its KNOWN mime (TaskFile.mime_type), not by
    # guessing from the object key - the stored key isn't always suffixed.
    data = _read_bytes(task_file.object_key)
    if data is None:
        return _persist_check(task_file, provider, created_by_id, [Finding(
            severity="info", category="spec",
            message="Couldn't read this file for automated QA — review it "
                    "manually.")])
    media = [MediaInput(data=data, mime_type=mime, label=label)]

    brief = "\n".join(p for p in (getattr(task, "title", None),
                                  getattr(task, "description", None)) if p)
    deliverable = None
    d = getattr(task, "deliverable", None)
    if d is not None:
        deliverable = getattr(d, "deliverable_name", None)

    guidelines = []
    if client is not None:
        docs = (ClientAsset.query
                .filter_by(client_id=client.id, category="document")
                .limit(_MAX_GUIDELINES).all())
        guidelines = _load_media(
            [(a.object_key, a.original_filename) for a in docs],
            allowed={"application/pdf"})

    ctx = MediaCheckContext(
        brief=brief,
        deliverable=deliverable,
        brand_voice=getattr(client, "brand_voice", None),
        brand_notes=getattr(client, "brand_guidelines_notes", None),
        guidelines=guidelines,
        media=media,
    )
    findings = provider.check_media(ctx)
    return _persist_check(task_file, provider, created_by_id, findings)
