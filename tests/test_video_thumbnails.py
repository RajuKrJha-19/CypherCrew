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
