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
import os
import shutil
import subprocess
import tempfile
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

#: Design/document files (PSD, PDF, AI) run bigger than photos and have to
#: be pulled in full to render, so they get a higher ceiling; past it the
#: tile falls back to a format icon rather than downloading a giant file.
MAX_DOC_BYTES = 120 * 1024 * 1024

#: PSD is opened by Pillow; PDF/AI are rendered by PyMuPDF. Both matched on
#: mime OR extension, since browsers label these inconsistently (often
#: application/octet-stream).
_PSD_MIMES = {
    "image/vnd.adobe.photoshop", "image/x-photoshop", "image/psd",
    "application/x-photoshop", "application/photoshop", "application/psd",
    "application/x-adobe-photoshop",
}
_PDF_MIMES = {"application/pdf"}
_AI_MIMES = {
    "application/illustrator", "application/vnd.adobe.illustrator",
    "application/postscript",
}

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


#: Seconds into the clip we grab the poster frame from. A little past the
#: start skips the black/fade-in that many clips open on.
VIDEO_SEEK_SECONDS = 1.0

#: Hard ceiling on the ffmpeg call so a pathological file can't tie up a
#: worker thread indefinitely.
VIDEO_FFMPEG_TIMEOUT = 60

_ffmpeg_path = None
_ffmpeg_checked = False


def ffmpeg_path():
    """Path to ffmpeg if it's installed, else None (cached).

    Video thumbnails need a frame decode. Where ffmpeg is present (the
    production image) we generate a real webp once, in the background, and
    every tile then pulls that small cached image. Where it isn't (a bare
    dev box) video simply falls back to a client-side frame - supports()
    reports False, so the pipeline treats the clip as un-thumbnailable.
    """
    global _ffmpeg_path, _ffmpeg_checked
    if not _ffmpeg_checked:
        _ffmpeg_checked = True
        # Prefer a system ffmpeg; otherwise use the static binary shipped
        # by the imageio-ffmpeg wheel, so no apt/Docker step is needed for
        # video thumbnails to work on the server.
        path = shutil.which("ffmpeg")
        if not path:
            try:
                import imageio_ffmpeg
                path = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                path = None
        _ffmpeg_path = path
    return _ffmpeg_path


def _ext(task_file):
    name = (task_file.original_filename or "").lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def _is_video(task_file):
    return (task_file.mime_type or "").lower().startswith("video/")


def _is_psd(task_file):
    return (task_file.mime_type or "").lower() in _PSD_MIMES or _ext(task_file) == "psd"


def _is_pdf_like(task_file):
    """PDF, or an Illustrator file (modern .ai is PDF-compatible)."""
    mime = (task_file.mime_type or "").lower()
    return mime in _PDF_MIMES or mime in _AI_MIMES or _ext(task_file) in {"pdf", "ai"}


def _is_raster(task_file):
    """Something Pillow can open directly: a normal image, or a PSD (whose
    flattened composite Pillow reads)."""
    mime = (task_file.mime_type or "").lower()
    if mime.startswith("image/"):
        return mime not in {"image/svg+xml"}
    return _is_psd(task_file)


def _size_cap(task_file):
    return MAX_DOC_BYTES if (_is_pdf_like(task_file) or _is_psd(task_file)) else MAX_SOURCE_BYTES


def supports(task_file):
    """True when this app can actually render a thumbnail for the file.

    Raster images and PSD via Pillow (SVG excluded - it is markup Pillow
    won't open). PDF and Illustrator via PyMuPDF. Video via ffmpeg, but
    only when ffmpeg is actually installed.
    """
    if _is_video(task_file):
        return ffmpeg_path() is not None

    return _is_raster(task_file) or _is_pdf_like(task_file)


def _skip_may_be_stale(task_file):
    """True when a recorded `skipped` described this DEPLOYMENT, not this file.

    supports() reports False for video wherever ffmpeg is absent, and
    generate() writes that down as skipped - a terminal state. But "there
    is no ffmpeg here" is a fact about the box, not a verdict on the clip,
    and it stops being true the moment ffmpeg is installed. Nothing ever
    re-asked, so every video uploaded before then kept a permanent skipped
    row and the gallery showed it as a bare gradient forever.

    Deliberately narrow: a file skipped for decoding to too many pixels,
    or for a format nothing here renders, is a real decision and stays
    put. Only the tool-shaped skip is reconsidered, and only once ffmpeg
    can actually be found - a genuinely undecodable clip then lands on
    failed, which IS terminal, so this cannot become a retry loop.
    """
    return _is_video(task_file) and ffmpeg_path() is not None


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


class ThumbnailTooLarge(Exception):
    """Decodes to more pixels than this app is willing to process.

    A decision, not a failure: retrying will never produce a different
    answer, so the file is marked skipped rather than failed.
    """


def _render(source_bytes):
    """original bytes -> WEBP bytes, or None if it cannot be rendered.

    Raises only ThumbnailTooLarge. Everything else is swallowed and
    logged, because a thumbnail is a nice-to-have and an exception
    escaping here leaves the row stuck at "pending" - which means every
    later view retries the same doomed file.
    """
    from PIL import Image, ImageOps

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

    except Image.DecompressionBombError as error:
        # Pillow refused to decode it, which is the guard doing its job.
        # DecompressionBombError subclasses Exception directly - not
        # OSError or ValueError - so it used to slip past every except
        # clause here and in generate(), leaving the row at "pending"
        # and re-downloading a very large file on every gallery view.
        raise ThumbnailTooLarge(str(error)) from error

    except MemoryError as error:
        # Same class of problem, different symptom.
        raise ThumbnailTooLarge("not enough memory to decode") from error

    except Exception:
        # Truncated upload, not actually an image, an unsupported mode,
        # a codec that is not installed. Deliberately broad: the caller
        # needs an answer, not an exception.
        current_app.logger.warning(
            "Could not render thumbnail.", exc_info=True
        )
        return None


def _render_video(task_file):
    """Extract a poster frame from a video into WEBP bytes, via ffmpeg.

    ffmpeg reads the object over its presigned URL and seeks BEFORE the
    input (-ss before -i), so it pulls only the bytes around the chosen
    frame with HTTP range requests - it never downloads the whole clip.
    That is what makes a server-side video thumbnail cheap enough to do.
    Returns webp bytes, or None if ffmpeg is absent or the decode fails.
    """
    ff = ffmpeg_path()
    if not ff:
        return None

    try:
        url = StorageService().preview_url(
            object_key=task_file.object_key,
            expires_in=600,
        )
    except Exception:
        current_app.logger.warning(
            "No preview URL for video thumbnail of task file %s.",
            task_file.id, exc_info=True,
        )
        return None

    fd, tmp_path = tempfile.mkstemp(suffix=".webp")
    os.close(fd)

    cmd = [
        ff, "-y", "-loglevel", "error",
        "-ss", str(VIDEO_SEEK_SECONDS),   # seek before -i -> range read, not full download
        "-i", url,
        "-frames:v", "1",
        "-vf", f"scale=w={MAX_EDGE}:h={MAX_EDGE}:force_original_aspect_ratio=decrease",
        "-c:v", "libwebp", "-q:v", "80",
        tmp_path,
    ]

    try:
        subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=VIDEO_FFMPEG_TIMEOUT,
            check=True,
        )
        with open(tmp_path, "rb") as fh:
            data = fh.read()
        return data or None

    except Exception:
        # Clip shorter than the seek, unseekable input, an odd codec, a
        # timeout - any of these just means "no server-side frame", and
        # the tile falls back to a client-side one.
        current_app.logger.warning(
            "ffmpeg could not render a video thumbnail for task file %s.",
            task_file.id, exc_info=True,
        )
        return None

    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _render_pdf(source_bytes, task_file):
    """First page of a PDF/AI into WEBP bytes, via PyMuPDF.

    Modern Illustrator files are PDF-compatible, so they render the same
    way. The rendered page is handed to _render() to normalise + shrink to
    a webp, reusing the image path (and its safety limits).
    """
    try:
        import fitz  # PyMuPDF
    except Exception:
        current_app.logger.warning("PyMuPDF not available; cannot render PDF/AI.")
        return None

    filetype = "pdf" if _is_pdf_like(task_file) else None

    try:
        with fitz.open(stream=source_bytes, filetype=filetype) as doc:
            if doc.page_count < 1:
                return None
            page = doc.load_page(0)
            longest = max(page.rect.width, page.rect.height, 1)
            scale = min(MAX_EDGE / longest, 3)  # cap upscaling of tiny pages
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            png = pix.tobytes("png")
    except Exception:
        current_app.logger.warning(
            "Could not render PDF/AI thumbnail for task file %s.",
            task_file.id, exc_info=True,
        )
        return None

    return _render(png)


def generate(file_id, retry=False):
    """Build and store the thumbnail for one file.

    Safe to call from anywhere: it re-reads the row, decides whether
    there is anything to do, and records the outcome. Returns the
    resulting state.

    Terminal states are honoured, so calling this on a file that has
    already been decided does no work. That guarantee lives here rather
    than in the callers: the /thumb route only calls this for pending
    files, but relying on that meant one careless caller could
    re-download and re-decode a doomed file on every request.

    `retry` re-attempts a previous failure. Skipped files are not retried
    either - that state means "this cannot be thumbnailed" - with one
    exception: a video skipped on a box that had no ffmpeg was a statement
    about the box, and is reconsidered once ffmpeg is present. See
    _skip_may_be_stale.
    """
    task_file = db.session.get(TaskFile, file_id)

    if task_file is None:
        return None

    if task_file.thumbnail_state == STATE_READY and task_file.thumbnail_key:
        return STATE_READY

    if task_file.thumbnail_state == STATE_SKIPPED \
            and not _skip_may_be_stale(task_file):
        return STATE_SKIPPED

    if task_file.thumbnail_state == STATE_FAILED and not retry:
        return STATE_FAILED

    if not supports(task_file):
        task_file.thumbnail_state = STATE_SKIPPED
        db.session.commit()
        return STATE_SKIPPED

    # The size cap applies to anything pulled in full to render (images,
    # PSD, PDF/AI). Video is exempt - ffmpeg seeks over the URL and never
    # downloads the whole clip.
    if not _is_video(task_file) and (task_file.file_size or 0) > _size_cap(task_file):
        task_file.thumbnail_state = STATE_SKIPPED
        db.session.commit()
        return STATE_SKIPPED

    if not _claim(file_id):
        return task_file.thumbnail_state

    try:
        storage = StorageService()

        if _is_video(task_file):
            thumb = _render_video(task_file)
        elif _is_pdf_like(task_file):
            thumb = _render_pdf(storage.read_bytes(task_file.object_key), task_file)
        else:  # raster image or PSD
            thumb = _render(storage.read_bytes(task_file.object_key))

        if thumb is None:
            return _record_state(file_id, STATE_FAILED)

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

    except ThumbnailTooLarge as error:
        current_app.logger.info(
            "Skipping thumbnail for task file %s: %s", file_id, error
        )
        return _record_state(file_id, STATE_SKIPPED)

    except Exception:
        # Deliberately broad. Whatever went wrong, the row has to end up
        # in a terminal state: anything left at "pending" is retried by
        # the lazy path on every single view, so one bad file turns into
        # a download-and-fail loop that never stops on its own.
        # Genuinely transient failures are recoverable with
        # `flask thumbnails-backfill --retry-failed`.
        current_app.logger.exception(
            "Thumbnail generation failed for task file %s.", file_id
        )
        return _record_state(file_id, STATE_FAILED)

    finally:
        _release(file_id)


def _record_state(file_id, state):
    """Write a terminal state, on its own transaction.

    Re-reads the row rather than reusing the caller's instance: the
    failure path may have left the session in a state where that
    instance is stale or detached.
    """
    try:
        db.session.rollback()

        task_file = db.session.get(TaskFile, file_id)

        if task_file is not None:
            task_file.thumbnail_state = state
            db.session.commit()

    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Could not record thumbnail state %r for task file %s.",
            state, file_id,
        )

    return state


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


def thumbnail_is_missing(task_file):
    """True when the row claims a thumbnail that storage does not have.

    A HEAD, so this is never called while rendering a grid - see repair().
    A storage error answers False: "I could not check" must not be
    mistaken for "it is gone", or a blip would wipe a library's worth of
    perfectly good thumbnails back to pending.
    """
    if task_file is None:
        return False

    if task_file.thumbnail_state != STATE_READY or not task_file.thumbnail_key:
        return False

    try:
        return not StorageService().exists(object_key=task_file.thumbnail_key)
    except Exception:  # noqa: BLE001 - see docstring
        current_app.logger.warning(
            "Could not check whether thumbnail %s still exists.",
            task_file.thumbnail_key, exc_info=True,
        )
        return False


def forget_missing_thumbnail(file_id):
    """Put a row back to `pending` when its thumbnail is gone from storage.

    `ready` means "there is a thumbnail at this key". When the object is
    deleted underneath that row - a pruned bucket, a bucket swapped for a
    new one - the row goes on saying ready, file_thumbnail_url goes on
    handing out a presigned URL for it, and the grid renders an <img>
    pointing at a 404. The tile is broken and nothing anywhere disagrees:
    every layer is faithfully doing what it was told.

    Returns True when it reset the row. Costs one HEAD, so it belongs on
    an error path (the tile's onerror handler, or the verify sweep) and
    never in the render path.

    Resetting is all it does. Who rebuilds it differs by caller: a request
    schedules the work, a maintenance sweep does it inline. See repair().
    """
    task_file = db.session.get(TaskFile, file_id)

    if task_file is None or not thumbnail_is_missing(task_file):
        return False

    current_app.logger.info(
        "Thumbnail %s is missing from storage; resetting task file %s.",
        task_file.thumbnail_key, file_id,
    )

    # Back to undecided, so generate() does the work instead of trusting
    # the row and handing READY straight back.
    task_file.thumbnail_state = STATE_PENDING
    task_file.thumbnail_key = None
    db.session.commit()

    return True


def repair(file_id):
    """Reset a missing thumbnail and rebuild it inline.

    For maintenance (verify_ready, the CLI), where waiting for the work is
    the point. A request handler should call forget_missing_thumbnail and
    let the background pool pick it up instead - decoding a large PDF or
    PSD inside a request ties up a worker while a grid fires many tile
    requests at once.

    Returns the resulting state, or None when there was nothing to fix.
    """
    if not forget_missing_thumbnail(file_id):
        return None

    return generate(file_id)


def verify_ready(limit=None):
    """Find `ready` rows whose object is gone, and rebuild them.

    One HEAD per ready file, so this is a maintenance sweep and not
    something any request path runs. Returns a count per resulting state.
    """
    query = (
        TaskFile.query
        .filter(TaskFile.thumbnail_state == STATE_READY)
        .filter(TaskFile.thumbnail_key.isnot(None))
        .order_by(TaskFile.created_at.desc())
    )

    if limit:
        query = query.limit(limit)

    counts = {}

    for task_file in query.all():
        state = repair(task_file.id)
        if state is not None:
            counts[state] = counts.get(state, 0) + 1

    return counts


def backfill(limit=None, retry_failed=False):
    """Generate thumbnails for files that predate this feature.

    Runs inline rather than through the pool so the caller can watch it
    finish. Returns a count per resulting state.

    Skipped rows are swept in too, but only where ffmpeg has since made
    them answerable (see _skip_may_be_stale) - the videos that were
    uploaded before it was installed. generate() re-checks each one and
    puts a genuinely undecodable clip on failed, so nothing here can spin.
    """
    from sqlalchemy import and_, or_

    states = [STATE_PENDING] + ([STATE_FAILED] if retry_failed else [])

    wanted = TaskFile.thumbnail_state.in_(states)

    # Narrowed to video rather than every skipped row: an oversized image
    # is skipped for a reason that has not changed, and sweeping the whole
    # skipped set into a large library would read thousands of rows only
    # for generate() to decline each one.
    if ffmpeg_path() is not None:
        wanted = or_(wanted, and_(
            TaskFile.thumbnail_state == STATE_SKIPPED,
            TaskFile.mime_type.ilike("video/%"),
        ))

    query = (
        TaskFile.query
        .filter(wanted)
        .order_by(TaskFile.created_at.desc())
    )

    if limit:
        query = query.limit(limit)

    counts = {}

    for task_file in query.all():
        state = generate(task_file.id, retry=retry_failed)
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
    @click.option(
        # Named explicitly so the flag and the verify_ready() it calls can
        # share the word without the parameter shadowing the function.
        "--verify-ready", "verify",
        is_flag=True,
        default=False,
        help="Also check every file already marked ready and rebuild any "
             "whose thumbnail has gone missing from storage. One HEAD per "
             "file, so run it when a bucket has been pruned or swapped, "
             "not routinely.",
    )
    def _backfill_command(limit, retry_failed, verify):
        if verify:
            repaired = verify_ready(limit=limit)

            if repaired:
                for state, count in sorted(repaired.items(),
                                           key=lambda kv: str(kv[0])):
                    print(f"rebuilt (was missing) -> {state}: {count}")
            else:
                print("every ready thumbnail is present in storage")

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
