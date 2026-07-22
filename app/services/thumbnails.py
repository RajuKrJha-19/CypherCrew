"""Derived thumbnails for gallery and file lists.

Why this exists
---------------
The gallery pointed every tile at the original file: <img src=original>
for pictures and <video preload="metadata" src=original> for clips, the
latter purely to coax a poster frame out of the browser. Measured on a
single page of 21 files that came to roughly a gigabyte pulled from R2
to paint a grid of 150px squares - one 266 MB video accounted for
almost all of it.

Pictures now get a small WEBP generated once and reused. Video keeps
no server-side thumbnail: that needs a frame decode, ffmpeg is not a
dependency of this project, and fetching a 266 MB file to sample one
frame would trade a client-side cost for a worse server-side one. Video
tiles render as a static poster instead and only stream when actually
opened, which is where the payload really belonged.

How generation is scheduled
---------------------------
There is no Celery/Redis in this deployment, so "worker" here is a
small thread pool. Uploads hand the work off and return immediately;
the pool does the download-resize-upload out of band. Anything the pool
misses - files uploaded before this existed, a worker that died
mid-flight, a process restart - is picked up lazily the first time the
thumbnail is actually requested, so the two paths together mean a tile
never has to fall back to the original.
"""

import io
import threading
from concurrent.futures import ThreadPoolExecutor

from flask import current_app

from app.extensions import db
from app.models import TaskFile
from app.storage.storage_service import StorageService, StorageServiceError


#: Longest edge of a generated thumbnail. The gallery renders tiles at
#: ~150-220px; 512 keeps them crisp on a 2x display without turning the
#: thumbnail into a second full-size asset.
MAX_EDGE = 512

#: Beyond this an "image" is more likely to be something pathological
#: than a photo, and Pillow would happily try to decompress it.
MAX_SOURCE_BYTES = 40 * 1024 * 1024

#: Guards against decompression-bomb images.
MAX_PIXELS = 50_000_000

THUMBNAIL_CONTENT_TYPE = "image/webp"

STATE_PENDING = "pending"
STATE_READY = "ready"
STATE_SKIPPED = "skipped"
STATE_FAILED = "failed"

# Two workers is deliberate: this runs inside a gunicorn worker that is
# already serving requests, and thumbnailing is CPU work.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="thumbs")

# Stops the same file being generated twice when the background job and
# a lazy request race each other.
_inflight = set()
_inflight_lock = threading.Lock()


def supports(task_file):
    """True when this app can actually render a thumbnail for the file.

    SVG is excluded on purpose: it is markup, Pillow will not open it,
    and it is already refused a safe content-type on upload.
    """
    mime = (task_file.mime_type or "").lower()

    if not mime.startswith("image/"):
        return False

    return mime not in {"image/svg+xml"}


def thumbnail_key_for(task_file):
    """Deterministic key so a regenerated thumbnail replaces the old one."""
    return f"thumbnails/{task_file.id}.webp"


def _claim(file_id):
    with _inflight_lock:
        if file_id in _inflight:
            return False
        _inflight.add(file_id)
        return True


def _release(file_id):
    with _inflight_lock:
        _inflight.discard(file_id)


def _render(source_bytes):
    """original bytes -> WEBP bytes. Returns None if undecodable."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS

    try:
        with Image.open(io.BytesIO(source_bytes)) as img:
            # Phones store orientation in EXIF rather than in the pixels.
            img = ImageOps.exif_transpose(img)

            # WEBP cannot store every mode Pillow can open (P, CMYK,
            # I;16 ...), and RGBA is fine for it, so normalise.
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

            img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

            out = io.BytesIO()
            img.save(out, format="WEBP", quality=80, method=4)
            return out.getvalue()

    except (UnidentifiedImageError, OSError, ValueError):
        # Truncated upload, not really an image, or a decompression
        # bomb tripping MAX_IMAGE_PIXELS.
        return None


def generate(file_id):
    """Build and store the thumbnail for one file.

    Safe to call from anywhere: it re-reads the row, decides whether
    there is anything to do, and records the outcome. Returns the
    resulting state.
    """
    task_file = db.session.get(TaskFile, file_id)

    if task_file is None:
        return None

    if task_file.thumbnail_state == STATE_READY and task_file.thumbnail_key:
        return STATE_READY

    if not supports(task_file):
        task_file.thumbnail_state = STATE_SKIPPED
        db.session.commit()
        return STATE_SKIPPED

    if (task_file.file_size or 0) > MAX_SOURCE_BYTES:
        task_file.thumbnail_state = STATE_SKIPPED
        db.session.commit()
        return STATE_SKIPPED

    if not _claim(file_id):
        return task_file.thumbnail_state

    try:
        storage = StorageService()
        source = storage.read_bytes(task_file.object_key)

        thumb = _render(source)

        if thumb is None:
            task_file.thumbnail_state = STATE_FAILED
            db.session.commit()
            return STATE_FAILED

        key = thumbnail_key_for(task_file)

        storage.put_bytes(
            data=thumb,
            object_key=key,
            content_type=THUMBNAIL_CONTENT_TYPE,
        )

        task_file.thumbnail_key = key
        task_file.thumbnail_state = STATE_READY
        db.session.commit()

        return STATE_READY

    except (StorageServiceError, OSError):
        current_app.logger.exception(
            "Thumbnail generation failed for task file %s.", file_id
        )
        db.session.rollback()

        task_file.thumbnail_state = STATE_FAILED
        db.session.commit()
        return STATE_FAILED

    finally:
        _release(file_id)


def _run_in_app(app, file_id):
    with app.app_context():
        try:
            generate(file_id)
        except Exception:
            # A background thread that raises would otherwise die
            # silently and take the traceback with it.
            app.logger.exception(
                "Background thumbnail job crashed for task file %s.", file_id
            )
        finally:
            db.session.remove()


def schedule(file_id):
    """Queue generation without blocking the upload response."""
    if not file_id:
        return

    app = current_app._get_current_object()
    _executor.submit(_run_in_app, app, file_id)


# ---------------------------------------------------------------
# Upload hook
# ---------------------------------------------------------------
#
# Files are created by four different paths - the two task forms, the
# submission upload and the multipart completion - each committing in
# its own route. Hooking the session instead of those four call sites
# means a new upload route cannot forget to ask for a thumbnail.
#
# Ids are captured on flush, where the INSERT has run and the primary
# key exists, and only handed to the pool once the transaction has
# actually committed - a worker starting earlier could look for a row
# that is still invisible to it, or one a rollback is about to remove.

_SESSION_KEY = "cypher_pending_thumbnails"


def _remember_new_files(session, flush_context):
    pending = session.info.setdefault(_SESSION_KEY, [])

    for obj in session.new:
        if isinstance(obj, TaskFile) and obj.id and supports(obj):
            pending.append(obj.id)


def _dispatch_after_commit(session):
    pending = session.info.pop(_SESSION_KEY, [])

    for file_id in pending:
        schedule(file_id)


def _forget_after_rollback(session):
    session.info.pop(_SESSION_KEY, None)


def register_events(session):
    """Wire the upload hook onto the app's session."""
    from sqlalchemy import event

    event.listen(session, "after_flush", _remember_new_files)
    event.listen(session, "after_commit", _dispatch_after_commit)
    event.listen(session, "after_rollback", _forget_after_rollback)


def backfill(limit=None, retry_failed=False):
    """Generate thumbnails for files that predate this feature.

    Runs inline rather than through the pool so the caller can watch it
    finish. Returns a count per resulting state.
    """
    states = [STATE_PENDING] + ([STATE_FAILED] if retry_failed else [])

    query = (
        TaskFile.query
        .filter(TaskFile.thumbnail_state.in_(states))
        .order_by(TaskFile.created_at.desc())
    )

    if limit:
        query = query.limit(limit)

    counts = {}

    for task_file in query.all():
        state = generate(task_file.id)
        counts[state] = counts.get(state, 0) + 1

    return counts


def register_cli(app):
    """`flask thumbnails-backfill` - one-off for the existing library.

    Entirely optional: a file with no thumbnail still generates one the
    first time it is viewed. Running this just does the work up front
    instead of making the first viewer wait.
    """
    import click

    @app.cli.command("thumbnails-backfill")
    @click.option(
        "--limit",
        type=int,
        default=None,
        help="Process at most this many files, so a large library can "
             "be done in batches rather than one long run.",
    )
    @click.option(
        "--retry-failed",
        is_flag=True,
        default=False,
        help="Also retry files previously marked failed.",
    )
    def _backfill_command(limit, retry_failed):
        counts = backfill(limit=limit, retry_failed=retry_failed)

        if not counts:
            print("nothing to do - no files are pending")
            return

        for state, count in sorted(counts.items(), key=lambda kv: str(kv[0])):
            print(f"{state}: {count}")

        remaining = (
            TaskFile.query
            .filter(TaskFile.thumbnail_state == STATE_PENDING)
            .count()
        )
        print(f"still pending: {remaining}")
