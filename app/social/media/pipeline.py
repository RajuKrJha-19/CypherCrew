"""Resolve SocialMediaAsset rows into publish-ready MediaRefs.

A media item may point at a task submission file, a client brand asset, or
carry its own uploaded object key. This module normalizes all three to an
R2 object key + a presigned URL the provider adapters can hand to the
platform (or stream from).
"""

from app.extensions import db
from app.models import TaskFile, ClientAsset
from app.social.dto import MediaRef
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
        problems.append(f"{content.post_type} is not supported on this platform.")
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
