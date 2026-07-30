"""Video thumbnails, and the skip that was never revisited.

The gallery paints a video tile from a server-generated webp. Where ffmpeg
is missing, supports() reports False and generate() writes `skipped` - a
terminal state nothing ever retried. But "there is no ffmpeg here" is a
fact about the deployment, not a verdict on the clip, and it stopped being
true the moment imageio-ffmpeg landed in requirements.txt. Every video
uploaded before then kept its skipped row, file_thumbnail_url returned
None for it, and the tile stayed a bare gradient forever - with nothing
anywhere reporting a problem.
"""

import pytest

from app.services import thumbnails


@pytest.fixture(autouse=True)
def _no_background_dispatch(monkeypatch):
    """Silence the upload hook.

    register_events schedules a real generation on after_commit for every
    new TaskFile that supports() accepts, so simply creating a fixture row
    fires ffmpeg at an object key that was never uploaded - a background
    thread, a 25-second failure and a wall of logging, none of it the
    thing under test. These tests drive generate() directly.
    """
    monkeypatch.setattr(thumbnails, "schedule", lambda file_id: None)


class FakeFile:
    """Just enough of TaskFile for the predicates under test."""

    def __init__(self, mime="video/mp4", name="clip.mp4", size=1024):
        self.mime_type = mime
        self.original_filename = name
        self.file_size = size


# ----------------------------------------------------------------------
# _skip_may_be_stale - which skips are worth a second look
# ----------------------------------------------------------------------

def test_a_video_skip_is_stale_once_ffmpeg_exists(app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(thumbnails, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
        assert thumbnails._skip_may_be_stale(FakeFile()) is True


def test_a_video_skip_stands_while_ffmpeg_is_absent(app, monkeypatch):
    """Otherwise every gallery view re-asks a question with the same
    answer, on a box that will never be able to answer it."""
    with app.app_context():
        monkeypatch.setattr(thumbnails, "ffmpeg_path", lambda: None)
        assert thumbnails._skip_may_be_stale(FakeFile()) is False


@pytest.mark.parametrize("mime,name", [
    ("image/png", "huge.png"),          # skipped for decoding too large
    ("image/svg+xml", "logo.svg"),      # markup Pillow will not open
    ("application/zip", "assets.zip"),  # nothing renders this
])
def test_a_non_video_skip_is_never_reconsidered(app, monkeypatch, mime, name):
    """Narrowness is the point. A file skipped for its own sake is a real
    decision; re-running it would download and re-decode the same doomed
    file on every pass."""
    with app.app_context():
        monkeypatch.setattr(thumbnails, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
        assert thumbnails._skip_may_be_stale(FakeFile(mime, name)) is False


# ----------------------------------------------------------------------
# supports() - the condition that produced the skip in the first place
# ----------------------------------------------------------------------

def test_video_is_unsupported_without_ffmpeg(app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(thumbnails, "ffmpeg_path", lambda: None)
        assert thumbnails.supports(FakeFile()) is False


def test_video_is_supported_with_ffmpeg(app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(thumbnails, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
        assert thumbnails.supports(FakeFile()) is True


def test_ffmpeg_is_actually_available_here(app):
    """imageio-ffmpeg is pinned in requirements.txt precisely so video
    thumbnails need no apt step. If this fails, every video uploaded from
    now on is skipped again and the tiles go back to bare gradients."""
    with app.app_context():
        assert thumbnails.ffmpeg_path() is not None, (
            "no ffmpeg - check that imageio-ffmpeg is installed"
        )


# ----------------------------------------------------------------------
# generate() - the guard that made skipped permanent
# ----------------------------------------------------------------------

def _video_row(session, make_task_file):
    return make_task_file(mime_type="video/mp4", filename="clip.mp4")


def test_generate_reconsiders_a_stale_video_skip(app, monkeypatch, session,
                                                 make_task_file):
    """The whole bug in one test: a skipped video, ffmpeg now present, and
    generate() must get as far as trying rather than returning early."""
    from app.extensions import db

    with app.app_context():
        row = make_task_file(mime_type="video/mp4", filename="clip.mp4")
        row.thumbnail_state = thumbnails.STATE_SKIPPED
        db.session.commit()

        monkeypatch.setattr(thumbnails, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")

        attempted = {"yes": False}

        def _render(task_file):
            attempted["yes"] = True
            return None          # undecodable -> terminal `failed`

        monkeypatch.setattr(thumbnails, "_render_video", _render)

        state = thumbnails.generate(row.id)

        assert attempted["yes"], (
            "generate() returned early on the skipped row - the stale skip "
            "is still terminal and the tile stays blank"
        )
        assert state == thumbnails.STATE_FAILED, (
            "a clip that cannot be decoded must land on failed, which IS "
            "terminal - otherwise this becomes a retry loop"
        )


def test_generate_still_short_circuits_a_skip_it_cannot_revisit(
        app, monkeypatch, session, make_task_file):
    from app.extensions import db

    with app.app_context():
        row = make_task_file(mime_type="video/mp4", filename="clip.mp4")
        row.thumbnail_state = thumbnails.STATE_SKIPPED
        db.session.commit()

        monkeypatch.setattr(thumbnails, "ffmpeg_path", lambda: None)

        called = {"yes": False}
        monkeypatch.setattr(thumbnails, "_render_video",
                            lambda f: called.__setitem__("yes", True))

        assert thumbnails.generate(row.id) == thumbnails.STATE_SKIPPED
        assert not called["yes"], "no ffmpeg, so nothing should be attempted"


def test_a_ready_video_is_left_alone(app, monkeypatch, session,
                                     make_task_file):
    """Reconsidering skips must not turn into regenerating everything."""
    from app.extensions import db

    with app.app_context():
        row = make_task_file(mime_type="video/mp4", filename="clip.mp4")
        row.thumbnail_state = thumbnails.STATE_READY
        row.thumbnail_key = "thumbnails/%s.webp" % row.id
        db.session.commit()

        monkeypatch.setattr(thumbnails, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")

        called = {"yes": False}
        monkeypatch.setattr(thumbnails, "_render_video",
                            lambda f: called.__setitem__("yes", True))

        assert thumbnails.generate(row.id) == thumbnails.STATE_READY
        assert not called["yes"]


# ----------------------------------------------------------------------
# `ready`, but the object is gone
# ----------------------------------------------------------------------
#
# The grid points <img> straight at a presigned URL for the generated webp,
# which is what keeps a large gallery fast. The cost is that nothing checks
# the object is still there: a row can say ready while the object has been
# deleted underneath it, and then every layer faithfully serves a URL to
# something that is not there. The tile renders broken and no log disagrees.


class FakeStorage:
    """Stands in for R2. `present` is the set of keys that exist."""

    def __init__(self, present=(), explode=False):
        self.present = set(present)
        self.explode = explode
        self.checked = []

    def exists(self, *, object_key):
        self.checked.append(object_key)
        if self.explode:
            raise RuntimeError("storage is unreachable")
        return object_key in self.present


def _ready_row(make_task_file, key="thumbnails/1.webp"):
    from app.extensions import db

    row = make_task_file(mime_type="image/png", filename="shot.png")
    row.thumbnail_state = thumbnails.STATE_READY
    row.thumbnail_key = key
    db.session.commit()
    return row


def test_a_missing_object_is_detected(app, monkeypatch, session,
                                      make_task_file):
    with app.app_context():
        row = _ready_row(make_task_file)
        monkeypatch.setattr(thumbnails, "StorageService",
                            lambda: FakeStorage(present=[]))

        assert thumbnails.thumbnail_is_missing(row) is True


def test_a_present_object_is_left_alone(app, monkeypatch, session,
                                        make_task_file):
    with app.app_context():
        row = _ready_row(make_task_file)
        monkeypatch.setattr(thumbnails, "StorageService",
                            lambda: FakeStorage(present=[row.thumbnail_key]))

        assert thumbnails.thumbnail_is_missing(row) is False
        assert thumbnails.forget_missing_thumbnail(row.id) is False
        assert row.thumbnail_state == thumbnails.STATE_READY


def test_an_unreachable_storage_never_reads_as_missing(app, monkeypatch,
                                                       session,
                                                       make_task_file):
    """"I could not check" must not be mistaken for "it is gone" - a blip
    would otherwise wipe a whole library back to pending and re-render it."""
    with app.app_context():
        row = _ready_row(make_task_file)
        monkeypatch.setattr(thumbnails, "StorageService",
                            lambda: FakeStorage(explode=True))

        assert thumbnails.thumbnail_is_missing(row) is False
        assert row.thumbnail_state == thumbnails.STATE_READY


def test_forgetting_puts_the_row_back_to_pending(app, monkeypatch, session,
                                                 make_task_file):
    with app.app_context():
        row = _ready_row(make_task_file)
        monkeypatch.setattr(thumbnails, "StorageService",
                            lambda: FakeStorage(present=[]))

        assert thumbnails.forget_missing_thumbnail(row.id) is True
        assert row.thumbnail_state == thumbnails.STATE_PENDING
        assert row.thumbnail_key is None, (
            "the stale key must go too, or file_thumbnail_url keeps handing "
            "out a URL for an object that is not there"
        )


def test_only_ready_rows_are_checked(app, monkeypatch, session,
                                     make_task_file):
    """A pending or failed row has no thumbnail to be missing, and a HEAD
    for each would be pure cost."""
    from app.extensions import db

    with app.app_context():
        row = make_task_file(mime_type="image/png", filename="shot.png")
        row.thumbnail_state = thumbnails.STATE_PENDING
        db.session.commit()

        storage = FakeStorage(present=[])
        monkeypatch.setattr(thumbnails, "StorageService", lambda: storage)

        assert thumbnails.thumbnail_is_missing(row) is False
        assert storage.checked == [], "storage should not have been asked"


def test_repair_rebuilds_after_forgetting(app, monkeypatch, session,
                                          make_task_file):
    with app.app_context():
        row = _ready_row(make_task_file)
        monkeypatch.setattr(thumbnails, "StorageService",
                            lambda: FakeStorage(present=[]))

        rebuilt = {"yes": False}

        def _generate(file_id, retry=False):
            rebuilt["yes"] = True
            return thumbnails.STATE_READY

        monkeypatch.setattr(thumbnails, "generate", _generate)

        assert thumbnails.repair(row.id) == thumbnails.STATE_READY
        assert rebuilt["yes"]


def test_repair_does_nothing_when_the_object_is_there(app, monkeypatch,
                                                      session,
                                                      make_task_file):
    with app.app_context():
        row = _ready_row(make_task_file)
        monkeypatch.setattr(thumbnails, "StorageService",
                            lambda: FakeStorage(present=[row.thumbnail_key]))
        monkeypatch.setattr(thumbnails, "generate", lambda *a, **k: "SHOULD NOT RUN")

        assert thumbnails.repair(row.id) is None


def test_the_gallery_tile_carries_a_repair_url():
    """The <img> needs somewhere to go when its presigned URL 404s, and
    thumb-repair.js is what wires it. Both halves, or neither works."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    gallery = (root / "app" / "templates" / "gallery" / "index.html").read_text(
        encoding="utf-8", errors="ignore")
    script = (root / "app" / "static" / "js" / "thumb-repair.js").read_text(
        encoding="utf-8", errors="ignore")

    assert "data-repair-src" in gallery, "the tile has no repair target"
    assert "thumb-repair.js" in gallery, "nothing wires the error handler"
    assert "repair=1" in gallery, (
        "the repair URL must carry ?repair=1 - without it the route serves "
        "the same dead presigned URL back"
    )

    # The script has to read the attribute the template writes, and must
    # bind exactly once or a still-broken tile retries in a loop.
    assert "repairSrc" in script
    assert "repairBound" in script


def test_a_forged_repair_request_cannot_reset_a_healthy_row(
        app, monkeypatch, session, make_task_file):
    """?repair=1 is reachable by anyone who can see the file, so the route
    must check storage rather than take the caller's word for it."""
    with app.app_context():
        row = _ready_row(make_task_file)
        storage = FakeStorage(present=[row.thumbnail_key])
        monkeypatch.setattr(thumbnails, "StorageService", lambda: storage)

        # The object is present, so the row must survive the request intact.
        assert thumbnails.forget_missing_thumbnail(row.id) is False
        assert row.thumbnail_state == thumbnails.STATE_READY
        assert storage.checked == [row.thumbnail_key]
