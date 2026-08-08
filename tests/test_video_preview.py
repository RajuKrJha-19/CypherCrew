"""720p faststart video-preview generation: a video gets a preview when ffmpeg
is present, a non-video / no-ffmpeg is skipped (so playback falls back to the
original), and the state machine is idempotent. generate() is exercised
directly; background scheduling is stubbed so no real ffmpeg ever runs here.
"""
import pytest

from app.extensions import db
from app.models import TaskFile
from app.services import video_preview
from app.social.media import transcode


@pytest.fixture(autouse=True)
def _no_bg_generation(monkeypatch):
    # Stop the upload session-hooks from spawning real thumbnail/preview ffmpeg
    # jobs on the (fake) test object keys — these tests drive generate() directly.
    from app.services import thumbnails
    monkeypatch.setattr(thumbnails, "schedule", lambda *a, **k: None)
    monkeypatch.setattr(video_preview, "schedule", lambda *a, **k: None)


def test_generate_ready_when_ffmpeg_makes_a_preview(app, make_task_file, monkeypatch):
    tf = make_task_file(mime_type="video/mp4")
    monkeypatch.setattr(transcode, "available", lambda: True)
    monkeypatch.setattr(transcode, "make_preview", lambda key: "previews/abc.mp4")
    with app.app_context():
        assert video_preview.generate(tf.id) == "ready"
        row = db.session.get(TaskFile, tf.id)
        assert row.preview_key == "previews/abc.mp4"
        assert row.preview_state == "ready"


def test_generate_failed_when_ffmpeg_returns_nothing(app, make_task_file, monkeypatch):
    tf = make_task_file(mime_type="video/mp4")
    monkeypatch.setattr(transcode, "available", lambda: True)
    monkeypatch.setattr(transcode, "make_preview", lambda key: None)
    with app.app_context():
        assert video_preview.generate(tf.id) == "failed"
        assert db.session.get(TaskFile, tf.id).preview_key is None


def test_generate_skips_a_non_video(app, make_task_file, monkeypatch):
    tf = make_task_file(mime_type="image/png", filename="a.png")
    monkeypatch.setattr(transcode, "available", lambda: True)
    with app.app_context():
        assert video_preview.generate(tf.id) == "skipped"


def test_generate_skips_without_ffmpeg(app, make_task_file, monkeypatch):
    tf = make_task_file(mime_type="video/mp4")
    monkeypatch.setattr(transcode, "available", lambda: False)
    with app.app_context():
        assert video_preview.generate(tf.id) == "skipped"


def test_generate_is_idempotent_when_already_ready(app, make_task_file, monkeypatch):
    tf = make_task_file(mime_type="video/mp4")
    monkeypatch.setattr(transcode, "available", lambda: True)
    calls = []
    monkeypatch.setattr(transcode, "make_preview",
                        lambda key: calls.append(1) or "previews/x.mp4")
    with app.app_context():
        assert video_preview.generate(tf.id) == "ready"
        assert video_preview.generate(tf.id) == "ready"   # second call: no work
    assert len(calls) == 1
