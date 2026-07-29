"""On-publish video downscaling: fit a too-wide-but-otherwise-fine video to
the platform's width, and never use a resize to mask a real problem."""
from app.social.dto import MediaRef, PostContent
from app.social.media import fit, transcode
from app.social.providers.meta_instagram import MetaInstagramProvider

_CAPS = MetaInstagramProvider.capabilities
_REEL = _CAPS.spec_for("reel")


# -- fit.downscale_target_width --------------------------------------------

def test_width_is_the_only_problem_returns_the_target_width():
    # 2160x3840 9:16 clip: correct shape, just too many pixels.
    meta = {"width": 2160, "height": 3840, "duration": 12,
            "fps": 30, "codec": "h264"}
    assert fit.downscale_target_width(_REEL, meta) == 1920


def test_a_file_already_within_width_needs_no_resize():
    meta = {"width": 1080, "height": 1920, "duration": 12,
            "fps": 30, "codec": "h264"}
    assert fit.downscale_target_width(_REEL, meta) is None


def test_resize_is_refused_when_something_else_is_also_wrong():
    # Too wide AND too long: the re-encode fixes the width but not the
    # 20-minute duration, so it must not be offered as a fix.
    meta = {"width": 2160, "height": 3840, "duration": 20 * 60,
            "fps": 30, "codec": "h264"}
    assert fit.downscale_target_width(_REEL, meta) is None


def test_resize_also_fixes_oversize_and_wrong_codec():
    # The reported production file: 2160px wide, 447MB, and a codec the reel
    # spec doesn't list. The transcode re-encodes to h264 at a controlled
    # bitrate, so it fixes width + size + codec in one pass - it must be
    # offered as fixable, not rejected on a naive byte estimate.
    meta = {"width": 2160, "height": 3840, "duration": 30, "fps": 30,
            "codec": "vp9", "bytes": 447 * 1024 * 1024}
    assert fit.downscale_target_width(_REEL, meta) == 1920


# -- the Instagram story spec (previously missing) -------------------------

def test_story_now_has_a_spec_and_catches_an_oversized_file():
    spec = _CAPS.spec_for("story")
    assert spec is not None
    problems = fit.check_spec(spec, {"width": 2160, "height": 3840})
    assert any("2160px wide" in p for p in problems)
    # ...and a resize is the right fix for it.
    assert fit.downscale_target_width(spec, {"width": 2160, "height": 3840}) \
        == 1920


def test_a_normal_story_still_passes():
    spec = _CAPS.spec_for("story")
    assert fit.check_spec(spec, {"width": 1080, "height": 1920}) == []


# -- transcode.fit_content --------------------------------------------------

def _content():
    return PostContent(
        platform="instagram", post_type="reel",
        media=[MediaRef(object_key="social_uploads/big.mp4",
                        mime_type="video/mp4",
                        measurements={"width": 2160, "height": 3840,
                                      "duration": 12, "fps": 30,
                                      "codec": "h264"})])


def test_fit_content_downscales_an_oversized_video(monkeypatch):
    monkeypatch.setattr(transcode, "available", lambda: True)
    monkeypatch.setattr(
        transcode, "_downscale",
        lambda key, w, meas: ("social_uploads/derived/x.mp4",
                              {"width": w, "height": 3413, "codec": "h264"}))
    content = _content()
    n = transcode.fit_content(content, _CAPS)
    assert n == 1
    assert content.media[0].object_key == "social_uploads/derived/x.mp4"
    assert content.media[0].measurements["width"] == 1920


def test_fit_content_is_a_noop_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(transcode, "available", lambda: False)
    content = _content()
    n = transcode.fit_content(content, _CAPS)
    assert n == 0
    assert content.media[0].object_key == "social_uploads/big.mp4"


def test_fit_content_leaves_a_fitting_video_alone(monkeypatch):
    monkeypatch.setattr(transcode, "available", lambda: True)
    called = []
    monkeypatch.setattr(transcode, "_downscale",
                        lambda *a: called.append(a) or None)
    content = _content()
    content.media[0].measurements = {"width": 1080, "height": 1920,
                                     "duration": 12, "fps": 30,
                                     "codec": "h264"}
    n = transcode.fit_content(content, _CAPS)
    assert n == 0
    assert called == []   # never even attempted a resize


# -- schedule_post: oversized reel with / without ffmpeg --------------------

def _oversized_reel_post():
    from app.extensions import db
    from app.models import (SocialAccount, SocialPost, SocialPostTarget,
                            SocialMediaAsset)
    acct = SocialAccount(platform="instagram", external_id="TR-1",
                         display_name="ig", account_type="ig_business",
                         status="active")
    post = SocialPost(title="big reel", status="approved", base_caption="c")
    db.session.add_all([acct, post])
    db.session.flush()
    t = SocialPostTarget(social_post_id=post.id, social_account_id=acct.id,
                         platform="instagram", post_type="reel",
                         status="draft")
    db.session.add(t)
    db.session.flush()
    db.session.add(SocialMediaAsset(
        target_id=t.id, source="upload", role="main", sort_order=0,
        object_key="social_uploads/big.mp4",
        meta={"measurements": {"width": 2160, "height": 3840,
                               "duration": 30, "fps": 30, "codec": "h264"}}))
    db.session.commit()
    return post, t


def test_oversized_reel_blocks_with_ffmpeg_hint_without_ffmpeg(
        session, monkeypatch):
    from app.extensions import db
    from app.social.services import publishing
    monkeypatch.setattr(transcode, "available", lambda: False)
    post, t = _oversized_reel_post()

    publishing.schedule_post(post, actor_id=None)
    db.session.refresh(t)
    assert t.status == "blocked"
    assert "ffmpeg is not installed" in (t.last_error or "")


def test_oversized_reel_schedules_when_ffmpeg_present(session, monkeypatch):
    from app.extensions import db
    from app.social.services import publishing
    monkeypatch.setattr(transcode, "available", lambda: True)
    post, t = _oversized_reel_post()

    publishing.schedule_post(post, actor_id=None)
    db.session.refresh(t)
    assert t.status == "scheduled"          # worker will resize on publish
    assert t.last_error is None
