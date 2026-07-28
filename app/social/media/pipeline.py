"""Resolve SocialMediaAsset rows into publish-ready MediaRefs.

A media item may point at a task submission file, a client brand asset, or
carry its own uploaded object key. This module normalizes all three to an
R2 object key + a presigned URL the provider adapters can hand to the
platform (or stream from).
"""

from app.extensions import db
from app.models import TaskFile, ClientAsset
from app.social.dto import MediaRef
from app.social.media import fit
from app.storage.storage_service import StorageService


def _resolve_key(asset):
    if asset.object_key:
        return asset.object_key
    if asset.task_file_id:
        tf = db.session.get(TaskFile, asset.task_file_id)
        if tf:
            return tf.object_key
    if asset.client_asset_id:
        ca = db.session.get(ClientAsset, asset.client_asset_id)
        if ca:
            return ca.object_key
    return None


def resolve_media(assets) -> list[MediaRef]:
    refs = []
    for asset in sorted(assets, key=lambda a: a.sort_order):
        key = _resolve_key(asset)
        if not key:
            continue
        refs.append(MediaRef(
            object_key=key,
            mime_type=asset.mime_type,
            role=asset.role,
            sort_order=asset.sort_order,
            alt_text=asset.alt_text,
            source=asset.source,
            # Measured in the composer and stored on the asset; carried
            # through so validation can quote real numbers rather than
            # guessing from the post type alone.
            measurements=(asset.meta or {}).get("measurements") or {},
        ))
    return refs


def presigned_url(object_key, expires_in=3600):
    """A short-lived URL the platform can fetch the media from (Meta/YouTube
    pull media by URL; large files stream directly from R2, never through
    the app)."""
    return StorageService().preview_url(
        object_key=object_key, expires_in=expires_in
    )


def validate_against(capabilities, content) -> list[str]:
    """Generic, platform-agnostic media checks derived from Capabilities.
    Adapters can add platform-specific checks in their own validate()."""
    problems = []
    if capabilities is None:
        return problems
    if not capabilities.supports(content.post_type):
        alternatives = sorted(capabilities.post_types or [])
        hint = f" This platform takes: {', '.join(alternatives)}." \
            if alternatives else ""
        problems.append(
            f"{content.post_type} is not supported on this platform.{hint}")
    else:
        # The type IS supported - now does the actual file meet its spec?
        # This is what turns "video is not supported" into "reels are
        # 3s-15min and this is 2s", which is the sentence that gets the
        # source file fixed. Unmeasured media adds nothing here; the
        # platform judges it and its real error is surfaced.
        meta = content.media[0].measurements if content.media else {}
        for reason in fit.check_spec(
                capabilities.spec_for(content.post_type), meta):
            problems.append(
                f"This {content.post_type} can't publish here: {reason}.")
    if content.post_type == "carousel" and capabilities.max_carousel:
        n = len(content.media)
        if n < 2:
            problems.append("A carousel needs at least 2 items.")
        elif n > capabilities.max_carousel:
            problems.append(
                f"A carousel allows at most {capabilities.max_carousel} items "
                f"(got {n})."
            )
    if capabilities.max_caption_chars and content.caption:
        if len(content.caption) > capabilities.max_caption_chars:
            problems.append(
                f"Caption exceeds {capabilities.max_caption_chars} characters."
            )
    return problems
