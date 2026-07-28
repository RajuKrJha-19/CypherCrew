"""ffprobe as the backstop behind the browser's measurement.

The browser covers most posts and is instant, but it cannot open a .mov or
HEVC deliverable - which is exactly what video editors hand over - and no
browser can report frame rate or codec, both of which Facebook Reels
require.

The rule that matters more than any of that: a probe failing must never
stop a post. A missing binary, a slow read, an odd container - all mean
"unmeasured", which the rest of the system already handles by letting the
platform judge.
"""

import subprocess

import pytest

from app.social.media import probe

FFPROBE_JSON = b"""{
  "streams": [
    {"codec_type": "audio", "codec_name": "aac"},
    {"codec_type": "video", "codec_name": "h264", "width": 1080,
     "height": 1920, "avg_frame_rate": "30000/1001"}
  ],
  "format": {"duration": "42.5", "size": "10485760"}
}"""


class _Result:
    def __init__(self, returncode=0, stdout=b"{}"):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


@pytest.fixture()
def with_ffprobe(monkeypatch):
    monkeypatch.setattr(probe, "available", lambda: True)


# --------------------------------------------------------------------------
# Reading what a browser cannot
# --------------------------------------------------------------------------

def test_a_probe_reports_everything_the_browser_cannot(
        app, monkeypatch, with_ffprobe):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Result(stdout=FFPROBE_JSON))

    with app.app_context():
        out = probe.probe_url("https://example.invalid/clip.mov")

    assert out["width"] == 1080 and out["height"] == 1920
    assert out["duration"] == pytest.approx(42.5)
    assert out["bytes"] == 10485760
    # The two a <video> element cannot give you at all.
    assert out["codec"] == "h264"
    assert out["fps"] == pytest.approx(29.97, abs=0.01)


def test_a_fractional_frame_rate_is_read_as_a_number():
    """ffprobe reports 30000/1001, not 29.97."""
    assert probe._fps("30000/1001") == pytest.approx(29.97, abs=0.01)
    assert probe._fps("25/1") == 25
    assert probe._fps("0/0") is None
    assert probe._fps(None) is None
    assert probe._fps("weird") is None


# --------------------------------------------------------------------------
# It must never break a publish
# --------------------------------------------------------------------------

def test_no_ffprobe_installed_is_simply_unmeasured(app, monkeypatch):
    """The whole feature is optional - a host without ffmpeg behaves
    exactly as it did before."""
    monkeypatch.setattr(probe, "available", lambda: False)
    with app.app_context():
        assert probe.probe_url("https://example.invalid/clip.mp4") == {}


def test_a_failing_probe_returns_nothing_rather_than_raising(
        app, monkeypatch, with_ffprobe):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Result(returncode=1, stdout=b""))
    with app.app_context():
        assert probe.probe_url("https://example.invalid/clip.mp4") == {}


def test_a_timeout_is_not_an_error(app, monkeypatch, with_ffprobe):
    """A slow or huge file must not hang the scheduler or fail the post."""
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=25)

    monkeypatch.setattr(subprocess, "run", timeout)
    with app.app_context():
        assert probe.probe_url("https://example.invalid/huge.mp4") == {}


def test_unreadable_output_is_not_an_error(app, monkeypatch, with_ffprobe):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Result(stdout=b"not json at all"))
    with app.app_context():
        assert probe.probe_url("https://example.invalid/clip.mp4") == {}


def test_an_empty_url_probes_nothing(app, with_ffprobe):
    with app.app_context():
        assert probe.probe_url("") == {}
        assert probe.probe_url(None) == {}


def test_an_audio_only_file_yields_only_what_it_has(
        app, monkeypatch, with_ffprobe):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(
        stdout=b'{"streams":[{"codec_type":"audio"}],'
               b'"format":{"duration":"12.0"}}'))
    with app.app_context():
        out = probe.probe_url("https://example.invalid/voice.m4a")
    assert out == {"duration": 12.0}
    assert "width" not in out


# --------------------------------------------------------------------------
# The probe is bounded - it must not pull a 300MB file
# --------------------------------------------------------------------------

def test_the_probe_is_capped_and_times_out(app, monkeypatch, with_ffprobe):
    """Metadata, not a frame decode - which is why the reasoning against
    ffmpeg in thumbnails.py does not apply here."""
    seen = {}

    def capture(command, **kwargs):
        seen["command"] = command
        seen["timeout"] = kwargs.get("timeout")
        return _Result(stdout=FFPROBE_JSON)

    monkeypatch.setattr(subprocess, "run", capture)
    with app.app_context():
        probe.probe_url("https://example.invalid/clip.mp4")

    assert "-probesize" in seen["command"]
    assert "-analyzeduration" in seen["command"]
    assert seen["timeout"] and seen["timeout"] <= 60


# --------------------------------------------------------------------------
# ensure_measured: fill the gaps, keep what the browser already knew
# --------------------------------------------------------------------------

def _target_with_media(session, measurements=None):
    from app.models import SocialMediaAsset, SocialPost, SocialPostTarget

    post = SocialPost(title="probe me", status="draft")
    session.add(post)
    session.flush()
    target = SocialPostTarget(social_post_id=post.id, platform="fake",
                              post_type="video", status="draft")
    session.add(target)
    session.flush()
    session.add(SocialMediaAsset(
        target_id=target.id, source="upload", role="main", sort_order=0,
        object_key="social_uploads/clip.mov",
        meta=({"measurements": measurements} if measurements else None)))
    session.flush()
    return target


def test_the_browser_measurement_wins_where_it_exists(
        session, monkeypatch, with_ffprobe):
    """It measured the actual file the person selected. ffprobe only fills
    the gaps - here, the fps and codec it could not read."""
    from app.social.media import pipeline

    target = _target_with_media(session, {"width": 720, "height": 1280,
                                          "duration": 30})
    monkeypatch.setattr(pipeline, "presigned_url", lambda k: "https://x/clip")
    monkeypatch.setattr(probe, "probe_url",
                        lambda url: {"width": 999, "height": 999,
                                     "duration": 999, "fps": 30,
                                     "codec": "h264"})

    probe.ensure_measured(target)

    got = target.media[0].meta["measurements"]
    assert got["width"] == 720 and got["duration"] == 30     # browser's
    assert got["fps"] == 30 and got["codec"] == "h264"       # probe's


def test_a_file_the_browser_could_not_read_is_measured_by_the_probe(
        session, monkeypatch, with_ffprobe):
    """The .mov case this exists for."""
    from app.social.media import pipeline

    target = _target_with_media(session, measurements=None)
    monkeypatch.setattr(pipeline, "presigned_url", lambda k: "https://x/clip")
    monkeypatch.setattr(probe, "probe_url",
                        lambda url: {"width": 1080, "height": 1920,
                                     "duration": 20, "codec": "hevc"})

    probe.ensure_measured(target)

    got = target.media[0].meta["measurements"]
    assert got["width"] == 1080 and got["codec"] == "hevc"


def test_a_fruitless_probe_is_not_repeated(session, monkeypatch, with_ffprobe):
    """Otherwise every schedule attempt pays for the same failure."""
    from app.social.media import pipeline

    target = _target_with_media(session, measurements=None)
    monkeypatch.setattr(pipeline, "presigned_url", lambda k: "https://x/clip")

    calls = []
    monkeypatch.setattr(probe, "probe_url",
                        lambda url: calls.append(url) or {})

    probe.ensure_measured(target)
    probe.ensure_measured(target)

    assert len(calls) == 1


def test_ensure_measured_does_nothing_without_ffprobe(session, monkeypatch):
    monkeypatch.setattr(probe, "available", lambda: False)
    target = _target_with_media(session, measurements=None)
    probe.ensure_measured(target)        # must not raise
    assert (target.media[0].meta or {}).get("measurements") is None


def test_unreachable_storage_does_not_break_validation(
        session, monkeypatch, with_ffprobe):
    from app.social.media import pipeline

    target = _target_with_media(session, measurements=None)

    def boom(key):
        raise RuntimeError("R2 is down")

    monkeypatch.setattr(pipeline, "presigned_url", boom)
    probe.ensure_measured(target)        # must not raise
