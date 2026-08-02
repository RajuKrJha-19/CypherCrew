"""The façade the routes call. Gathers context from the DB + object storage,
invokes the resolved provider, and returns plain dicts/strings.

Media is read as bytes and size-capped here so a huge asset never inflates an
AI request; unreadable or oversized media is skipped, and generation still
proceeds on the brief + brand text alone.
"""
import mimetypes
import time

from flask import current_app

from app.ai.base import (
    CaptionContext, Finding, MediaCheckContext, MediaInput,
)
from app.ai.errors import AITransient
from app.ai.registry import get_provider

# One bounded retry on a transient failure (rate limit / 5xx). These calls are
# human-triggered and low-volume, so a single short backoff is plenty. We do
# NOT fall back to another provider on failure - that would double both the
# cost and the failure surface for a tool where the user can just click again.
_RETRY_BACKOFF_S = 0.75


def _invoke(call):
    try:
        return call()
    except AITransient:
        time.sleep(_RETRY_BACKOFF_S)
        return call()

# Vision inputs we send for captions/alt-text. Video is intentionally excluded
# (heavy + not needed to caption from the brief). Media QA also accepts PDFs
# (design deliverables + brand-guideline docs).
_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_QA_MIMES = _IMAGE_MIMES | {"application/pdf"}
# At most this many brand-guideline docs are fed to a QA call (cost bound).
_MAX_GUIDELINES = 2
# At most this many official-logo reference images per QA call (cost bound).
_MAX_LOGO_REFS = 2

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


def _shrink_image(data, mime_type):
    """Downscale an oversized image for the AI vision call ONLY. This never
    touches the stored original that gets published - it shrinks the throwaway
    copy sent to the model, cutting vision tokens, latency and rate-limit
    pressure on big creatives. Only images past the configured longest edge are
    touched (standard 1080px creatives pass through untouched, so quality is
    preserved). Best-effort: any failure (or Pillow absent) returns the
    original bytes unchanged. Returns (data, mime_type)."""
    if not mime_type or not mime_type.startswith("image/") or mime_type == "image/gif":
        return data, mime_type
    max_dim = int(current_app.config.get("AI_IMAGE_MAX_DIM", 1568) or 0)
    if max_dim <= 0:
        return data, mime_type
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data))
        if max(img.size) <= max_dim:
            return data, mime_type               # already small enough
        img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim))        # keeps aspect ratio, downscale only
        buf = BytesIO()
        # Quality 88 keeps text/logos legible for QA while shrinking a lot.
        img.save(buf, format="JPEG", quality=88)
        return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - a resize must never break generation
        return data, mime_type


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
        sdata, smime = _shrink_image(data, _mime_for(object_key))
        out.append(MediaInput(data=sdata, mime_type=smime, label=label))
    return out


# -- public API -------------------------------------------------------------

def _log_usage(provider, feature, actor_id, client_id, status="ok"):
    """Record what a provider call cost (best-effort; never raises). Returns the
    usage row id (so a caller can later record whether the output was kept)."""
    from app.ai import usage
    return usage.record(
        feature=feature, provider=provider.key,
        model=(getattr(provider, "model", None) or provider.key),
        input_tokens=provider.usage.get("input_tokens", 0),
        output_tokens=provider.usage.get("output_tokens", 0),
        status=status, actor_id=actor_id, client_id=client_id)


def generate_caption(*, brief="", industry=None, brand_voice=None,
                     brand_notes=None, facts=None, tone=None,
                     platforms=None, media=None, actor_id=None, client_id=None):
    """media: iterable of (object_key, label). Returns a plain dict."""
    provider = get_provider("caption")
    ctx = CaptionContext(
        brief=brief or "",
        industry=industry,
        brand_voice=brand_voice,
        brand_notes=brand_notes,
        facts=facts or None,
        tone=tone or None,
        platforms=list(platforms or []),
        caption_limits=caption_limits(platforms or []),
        media=_load_media(media or []),
    )
    try:
        result = _invoke(lambda: provider.generate_caption(ctx))
    except Exception:
        _log_usage(provider, "caption", actor_id, client_id, status="error")
        raise
    usage_id = _log_usage(provider, "caption", actor_id, client_id)
    return {
        "caption": result.caption,
        "per_platform": result.per_platform,
        "hashtags": result.hashtags,
        "first_comment": result.first_comment,
        "variations": result.variations,
        "ai_usage_id": usage_id,
    }


def generate_alt_text(object_key, label=None, actor_id=None):
    """One alt-text line for a single image object, or "" if it can't be read."""
    provider = get_provider("caption")
    images = _load_media([(object_key, label)])
    if not images:
        return ""
    try:
        alt = _invoke(lambda: provider.generate_alt_text(images[0]))
    except Exception:
        _log_usage(provider, "alt_text", actor_id, None, status="error")
        raise
    _log_usage(provider, "alt_text", actor_id, None)
    return alt


def generate_reply(*, review_text="", rating=None, reviewer=None,
                   business_name=None, brand_voice=None, brand_notes=None,
                   facts=None, actor_id=None, client_id=None):
    """Draft an on-brand reply to a Google review. Returns the reply string."""
    from app.ai.base import ReplyContext
    provider = get_provider("reply")
    ctx = ReplyContext(
        review_text=review_text or "",
        rating=rating,
        reviewer=reviewer,
        business_name=business_name,
        brand_voice=brand_voice,
        brand_notes=brand_notes,
        facts=facts or None,
    )
    try:
        reply = _invoke(lambda: provider.generate_reply(ctx))
    except Exception:
        _log_usage(provider, "reply", actor_id, client_id, status="error")
        raise
    _log_usage(provider, "reply", actor_id, client_id)
    return reply


def generate_comment_reply(*, comment_text="", author=None, business_name=None,
                           brand_voice=None, brand_notes=None, facts=None,
                           post_context=None, actor_id=None, client_id=None):
    """Draft an on-brand reply to a comment on a published social post. Uses
    the caption (general text) tier - comments are short, lightweight text -
    and returns the reply string. Draft-only: the human edits and posts it."""
    from app.ai.base import ReplyContext
    provider = get_provider("caption")
    ctx = ReplyContext(
        kind="comment",
        review_text=comment_text or "",
        reviewer=author,
        business_name=business_name,
        brand_voice=brand_voice,
        brand_notes=brand_notes,
        facts=facts or None,
        post_context=post_context or None,
    )
    try:
        reply = _invoke(lambda: provider.generate_reply(ctx))
    except Exception:
        _log_usage(provider, "comment", actor_id, client_id, status="error")
        raise
    _log_usage(provider, "comment", actor_id, client_id)
    return reply


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
    data, mime = _shrink_image(data, mime)
    media = [MediaInput(data=data, mime_type=mime, label=label)]

    brief = "\n".join(p for p in (getattr(task, "title", None),
                                  getattr(task, "description", None)) if p)
    deliverable = None
    d = getattr(task, "deliverable", None)
    if d is not None:
        deliverable = getattr(d, "deliverable_name", None)

    guidelines = []
    references = []
    facts = ""
    if client is not None:
        docs = (ClientAsset.query
                .filter_by(client_id=client.id, category="document")
                .limit(_MAX_GUIDELINES).all())
        guidelines = _load_media(
            [(a.object_key, a.original_filename) for a in docs],
            allowed={"application/pdf"})
        # The client's official logo image(s) - the correct-logo reference the
        # checker compares the deliverable against (wrong / altered / outdated).
        logos = (ClientAsset.query
                 .filter_by(client_id=client.id, category="logo")
                 .limit(_MAX_LOGO_REFS).all())
        references = _load_media(
            [(a.object_key, "official logo") for a in logos],
            allowed=_IMAGE_MIMES)
        from app.ai import client_brain
        facts = client_brain.facts_text(client)

    ctx = MediaCheckContext(
        brief=brief,
        deliverable=deliverable,
        brand_voice=getattr(client, "brand_voice", None),
        brand_notes=getattr(client, "brand_guidelines_notes", None),
        facts=facts or None,
        guidelines=guidelines,
        references=references,
        media=media,
    )
    client_id = getattr(client, "id", None)
    try:
        findings = _invoke(lambda: provider.check_media(ctx))
    except Exception:
        _log_usage(provider, "media_qa", created_by_id, client_id, status="error")
        raise
    _log_usage(provider, "media_qa", created_by_id, client_id)
    return _persist_check(task_file, provider, created_by_id, findings)
