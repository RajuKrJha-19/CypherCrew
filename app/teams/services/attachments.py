"""Files and images on a chat message.

Uploads stream straight through to R2 rather than being read into memory,
matching the Studio's own upload route. Two things bound them:

  * a hard byte cap enforced WHILE streaming, not just from the declared
    Content-Length - a client that lies about the length would otherwise
    walk straight past the check;
  * StorageService's content-type sanitiser, which rewrites `svg+xml` and
    `text/html` to `application/octet-stream`. That is not optional
    politeness: previews are served from presigned R2 URLs, so an SVG
    stored with its real type is stored XSS the moment anybody opens it.

Chat files live under their own `teams/` prefix, date-bucketed, so they are
trivially separable from client deliverables and social uploads - a
retention rule can expire old chat media without touching anything a client
paid for.
"""

import os
import re
from datetime import datetime
from uuid import uuid4

from flask import current_app

from app.extensions import db
from app.models import TeamAttachment
from app.storage.storage_service import StorageService

#: Images get rendered inline; everything else becomes a chip.
IMAGE_PREFIX = "image/"

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
MAX_FILENAME = 80


class AttachmentError(Exception):
    """Something the user did wrong, with a message fit to show them."""


class _TooLarge(Exception):
    """Raised by the counting wrapper. Never reaches a route."""


class _CappedStream:
    """A read-only file wrapper that refuses to yield more than `limit`.

    The declared Content-Length is a claim, not a fact. boto3 pulls from
    this object in chunks, so counting here is the only place the real size
    is known before it is already in the bucket.
    """

    def __init__(self, stream, limit):
        self._stream = stream
        self._limit = limit
        self.bytes_read = 0

    def read(self, size=-1):
        chunk = self._stream.read(size)
        if chunk:
            self.bytes_read += len(chunk)
            if self.bytes_read > self._limit:
                raise _TooLarge()
        return chunk


def max_bytes():
    return int(current_app.config.get("TEAMS_ATTACHMENT_MAX_MB", 25)) * 1024 * 1024


def safe_filename(filename):
    """A filename safe to put in an object key, never empty."""
    base = os.path.basename(filename or "")
    cleaned = _UNSAFE_NAME.sub("_", base)[:MAX_FILENAME].strip("._")
    return cleaned or "file"


def build_object_key(channel_id, filename):
    """`teams/channels/<id>/<yyyy>/<mm>/<uuid>-<name>`.

    Bucketed by channel then date: a channel's files stay together, and a
    lifecycle rule can drop an old month wholesale. The uuid is what makes
    two people uploading `logo.png` on the same day two objects.
    """
    now = datetime.utcnow()
    return (
        f"teams/channels/{int(channel_id)}/"
        f"{now:%Y}/{now:%m}/"
        f"{uuid4().hex}-{safe_filename(filename)}"
    )


def store(file_storage, channel):
    """Stream an uploaded file to R2 and return the details for a row.

    Takes a Werkzeug FileStorage. Raises AttachmentError with a message
    worth showing; never leaves a half-written object behind.
    """
    if file_storage is None or not file_storage.filename:
        raise AttachmentError("No file was provided.")

    limit = max_bytes()
    mb = limit // (1024 * 1024)

    # Cheap rejection first: if the browser already says it is too big,
    # refuse before opening a connection to R2.
    declared = getattr(file_storage, "content_length", None)
    if declared and declared > limit:
        raise AttachmentError(f"Files must be {mb} MB or smaller.")

    filename = safe_filename(file_storage.filename)
    object_key = build_object_key(channel.id, file_storage.filename)
    content_type = (file_storage.mimetype or "").lower() or None

    capped = _CappedStream(file_storage.stream, limit)

    try:
        StorageService().upload(
            file_obj=capped, object_key=object_key, content_type=content_type)
    except Exception as exc:                                 # noqa: BLE001
        # One handler, not three. StorageService re-raises everything as
        # StorageServiceError(...) from error, and boto3 wraps a reader's
        # exception too - so _TooLarge always arrives buried, and catching
        # it by type would silently never fire. Walk the chain instead.
        #
        # Discard unconditionally: a failed multipart upload can leave parts
        # behind, and deleting an object that was never written is free.
        discard(object_key)

        if _caused_by_too_large(exc):
            raise AttachmentError(f"Files must be {mb} MB or smaller.")

        current_app.logger.exception("[teams-upload] store failed")
        raise AttachmentError("Upload failed - please try again.")

    return {
        "object_key": object_key,
        "filename": filename,
        # The sanitised type is what R2 actually stores and therefore what
        # a preview will be served as - record that, not what was claimed,
        # so `is_image` can never disagree with the stored object.
        "content_type": StorageService._sanitize_content_type(content_type),
        "size_bytes": capped.bytes_read,
    }


def attach(message, uploads, commit=True):
    """Create TeamAttachment rows for an already-stored list of uploads."""
    rows = []
    for item in uploads:
        row = TeamAttachment(
            message_id=message.id,
            object_key=item["object_key"],
            filename=item["filename"],
            content_type=item.get("content_type"),
            size_bytes=item.get("size_bytes"),
            width=item.get("width"),
            height=item.get("height"),
        )
        db.session.add(row)
        rows.append(row)

    if commit:
        db.session.commit()
    return rows


def preview_url(attachment):
    """Short-lived presigned URL, or None.

    Best-effort by design: one unsignable object must not take out the
    whole message list, so a failure returns None and the bubble falls
    back to a chip.

    It LOGS, though. The first version swallowed silently and called
    StorageService.preview_url positionally - the argument is keyword-only,
    so every single image rendered as a file chip and nothing anywhere said
    why. A quiet fallback is fine; a quiet fallback that hides a TypeError
    is not.
    """
    try:
        return StorageService().preview_url(object_key=attachment.object_key)
    except Exception:                                        # noqa: BLE001
        current_app.logger.warning(
            "[teams] could not sign %s", attachment.object_key, exc_info=True)
        return None


def discard(object_key):
    """Remove a partially-written object. Failure here is not worth
    reporting - the media GC would collect it anyway."""
    try:
        StorageService().delete(object_key)
    except Exception:                                        # noqa: BLE001
        pass


def _caused_by_too_large(exc):
    """Whether `exc` is, or was caused by, the cap being hit."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, _TooLarge):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False
