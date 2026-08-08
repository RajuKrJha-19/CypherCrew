"""Small, faststart 720p web previews of VIDEO task files, for smooth playback.

A submitted deliverable can be 4K / 200 MB; played directly it buffers — its
bitrate outruns the connection and its moov atom sits at the END, so nothing
plays until the whole file downloads. We transcode a light 720p `+faststart`
preview ONCE, in the background on upload, and the player uses it when ready and
falls back to the untouched original whenever it isn't — so this can never
break playback. Mirrors the thumbnails service's background pattern.

Inert without ffmpeg: schedule() short-circuits, so on a box (or a test run)
with no ffmpeg nothing is spawned and every video is simply served as-is.
"""
from concurrent.futures import ThreadPoolExecutor

from flask import current_app

from app.extensions import db
from app.models import TaskFile
from app.social.media import transcode

STATE_PENDING = "pending"
STATE_READY = "ready"
STATE_SKIPPED = "skipped"
STATE_FAILED = "failed"

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="preview")
_SESSION_KEY = "cypher_pending_previews"


def is_video(task_file):
    return (task_file.mime_type or "").lower().startswith("video/")


def generate(file_id):
    """Build + store the 720p preview for one video task file, in a terminal-
    state-honouring, idempotent way. Records the outcome and NEVER raises to the
    caller. Returns the resulting state."""
    task_file = db.session.get(TaskFile, file_id)
    if task_file is None:
        return None
    if task_file.preview_state == STATE_READY and task_file.preview_key:
        return STATE_READY
    # skipped = not a video / no ffmpeg; failed = tried and couldn't. Reconsider
    # a skip only if it was a no-ffmpeg box that now has ffmpeg.
    if task_file.preview_state in (STATE_SKIPPED, STATE_FAILED):
        stale_skip = (task_file.preview_state == STATE_SKIPPED
                      and is_video(task_file) and transcode.available())
        if not stale_skip:
            return task_file.preview_state
    if not is_video(task_file) or not transcode.available():
        task_file.preview_state = STATE_SKIPPED
        db.session.commit()
        return STATE_SKIPPED

    try:
        key = transcode.make_preview(task_file.object_key)
    except Exception:  # noqa: BLE001 - a preview must never crash a thread
        current_app.logger.exception(
            "[preview] generation crashed for task file %s", file_id)
        key = None

    task_file = db.session.get(TaskFile, file_id)
    if task_file is None:                 # deleted while we transcoded
        return None
    if key:
        task_file.preview_key = key
        task_file.preview_state = STATE_READY
    else:
        task_file.preview_state = STATE_FAILED
    db.session.commit()
    return task_file.preview_state


def _run_in_app(app, file_id):
    with app.app_context():
        try:
            generate(file_id)
        except Exception:  # noqa: BLE001
            app.logger.exception(
                "[preview] background job crashed for task file %s", file_id)
        finally:
            db.session.remove()


def schedule(file_id):
    """Queue preview generation without blocking the request. A no-op when
    ffmpeg is unavailable, so nothing is spawned on a box (or a test run)
    without it."""
    if not file_id or not transcode.available():
        return
    app = current_app._get_current_object()
    _executor.submit(_run_in_app, app, file_id)


# -- upload hook: every new VIDEO task file gets a preview, whichever route
#    created it (ids captured on flush, dispatched only after the commit). ----

def _remember_new(session, flush_context):
    pending = session.info.setdefault(_SESSION_KEY, [])
    for obj in session.new:
        if isinstance(obj, TaskFile) and obj.id and is_video(obj):
            pending.append(obj.id)


def _dispatch_after_commit(session):
    for file_id in session.info.pop(_SESSION_KEY, []):
        schedule(file_id)


def register_events(session):
    from sqlalchemy import event
    event.listen(session, "after_flush", _remember_new)
    event.listen(session, "after_commit", _dispatch_after_commit)
