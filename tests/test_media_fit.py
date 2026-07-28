"""One file, every platform: what does it become, and why.

The reported bug: a 720x1280 video published on Facebook and was refused
for Instagram with "video is not supported on this platform". Instagram
never saw it - our own pre-flight rejected it, because Instagram's
post_types has no "video". Instagram takes that exact file happily, as a
Reel.

Removing the platform was the wrong answer for this agency: Facebook +
Instagram is ONE post in their client reports, and re-uploading media to
a stranded target lets someone publish a file that was never approved.

So the rule is reel-first with a video fallback, decided from the
platforms' real published limits.
"""

import pytest

from app.social.dto import Capabilities, MediaSpec
from app.social.media import fit
from app.social.providers.meta_facebook import MetaFacebookProvider
from app.social.providers.meta_instagram import MetaInstagramProvider

FB = MetaFacebookProvider.capabilities
IG = MetaInstagramProvider.capabilities

#: The file from the report: 9:16, comfortably inside every limit.
REPORTED = {"width": 720, "height": 1280, "duration": 30}


# --------------------------------------------------------------------------
# The reported file
# --------------------------------------------------------------------------

def test_the_reported_video_is_a_reel_on_both_platforms():
    """The whole point. It used to publish on Facebook and be refused by
    Instagram over a post-type name."""
    assert fit.choose_post_type("video", FB, REPORTED)[0] == "reel"
    assert fit.choose_post_type("video", IG, REPORTED)[0] == "reel"


def test_instagram_no_longer_refuses_a_perfectly_good_video():
    post_type, notes = fit.choose_post_type("video", IG, REPORTED)
    assert post_type is not None, notes


# --------------------------------------------------------------------------
# Where the two platforms genuinely differ
# --------------------------------------------------------------------------

def test_a_landscape_video_is_a_facebook_video_and_an_instagram_reel():
    """Facebook Reels are 9:16 only; Instagram's aspect range is wide
    enough to take landscape. Same file, two correct answers."""
    landscape = {"width": 1920, "height": 1080, "duration": 30}
    assert fit.choose_post_type("video", FB, landscape)[0] == "video"
    assert fit.choose_post_type("video", IG, landscape)[0] == "reel"


def test_a_long_video_falls_back_to_video_on_facebook_only():
    """Facebook Reels stop at 90 seconds, Instagram's run to 15 minutes."""
    long_clip = {"width": 1080, "height": 1920, "duration": 120}
    assert fit.choose_post_type("video", FB, long_clip)[0] == "video"
    assert fit.choose_post_type("video", IG, long_clip)[0] == "reel"


def test_a_downgrade_explains_itself():
    """Someone should know their Reel is going out as a plain video."""
    landscape = {"width": 1920, "height": 1080, "duration": 30}
    _, notes = fit.choose_post_type("video", FB, landscape)
    assert notes and "not a reel" in notes[0].lower()


def test_low_resolution_is_a_facebook_video_not_a_facebook_reel():
    """Facebook Reels need at least 540x960."""
    small = {"width": 360, "height": 640, "duration": 20}
    assert fit.choose_post_type("video", FB, small)[0] == "video"


# --------------------------------------------------------------------------
# When it genuinely cannot publish, say the number
# --------------------------------------------------------------------------

def test_a_two_second_clip_is_blocked_on_instagram_with_the_number():
    """Instagram has no video fallback, so a sub-3s clip really cannot go -
    and the message has to name the limit so the SOURCE file gets fixed."""
    tiny = {"width": 720, "height": 1280, "duration": 2}
    post_type, notes = fit.choose_post_type("video", IG, tiny)

    assert post_type is None
    assert notes
    assert "3" in notes[0], notes
    assert "2s" in notes[0], notes


def test_an_oversized_file_is_blocked_with_its_size():
    huge = {"width": 1080, "height": 1920, "duration": 60,
            "bytes": 400 * 1024 * 1024}
    post_type, notes = fit.choose_post_type("video", IG, huge)
    assert post_type is None
    assert "300MB" in notes[0], notes


def test_an_aspect_problem_names_the_actual_size():
    landscape = {"width": 1920, "height": 1080, "duration": 30}
    problems = fit.check_spec(FB.spec_for("reel"), landscape)
    assert any("1920x1080" in p and "9:16" in p for p in problems), problems


# --------------------------------------------------------------------------
# Never invent a failure
# --------------------------------------------------------------------------

def test_unmeasured_media_is_not_treated_as_broken():
    """No ffmpeg here, so plenty of files arrive unmeasured. Guessing
    would block content that is perfectly fine - the platform judges, and
    its real error is surfaced."""
    for meta in ({}, None, {"width": None, "height": None}):
        assert fit.choose_post_type("video", IG, meta)[0] is not None
        assert fit.choose_post_type("video", FB, meta)[0] is not None


def test_a_rounding_error_in_the_export_is_not_an_aspect_failure():
    """720x1281 is 9:16 to every human and to Meta."""
    assert not fit.check_spec(FB.spec_for("reel"),
                              {"width": 720, "height": 1281, "duration": 30})


def test_a_missing_spec_checks_nothing():
    assert fit.check_spec(None, REPORTED) == []
    assert fit.check_spec(MediaSpec(), REPORTED) == []


# --------------------------------------------------------------------------
# Non-video content is untouched
# --------------------------------------------------------------------------

def test_an_image_is_still_an_image():
    assert fit.choose_post_type("image", IG, {"width": 1080, "height": 1080})[0] \
        == "image"
    assert fit.choose_post_type("carousel", IG, {})[0] == "carousel"


def test_a_type_the_platform_cannot_take_at_all_is_blocked():
    gbp = Capabilities(post_types={"text", "image"})
    post_type, notes = fit.choose_post_type("carousel", gbp, {})
    assert post_type is None
    assert notes


def test_a_platform_with_only_video_takes_the_video():
    """YouTube has no reels concept in this engine - it must not be
    downgraded to nothing."""
    youtube = Capabilities(post_types={"video"})
    assert fit.choose_post_type("video", youtube, REPORTED)[0] == "video"


def test_no_capabilities_means_no_opinion():
    assert fit.choose_post_type("video", None, REPORTED)[0] == "video"


# --------------------------------------------------------------------------
# The composer's one-line summary
# --------------------------------------------------------------------------

def test_describe_labels_the_outcome():
    assert fit.describe("video", IG, REPORTED)["label"] == "Reel"
    assert fit.describe("video", FB, REPORTED)["ok"] is True

    tiny = {"width": 720, "height": 1280, "duration": 2}
    blocked = fit.describe("video", IG, tiny)
    assert blocked["ok"] is False
    assert blocked["notes"]


# --------------------------------------------------------------------------
# Meta's own content rejections are permanent, not retried forever
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", [1363040, 1363127])
def test_a_file_meta_rejects_is_permanent(code):
    """Aspect ratio (1363040) and resolution (1363127). Retrying the same
    bytes fails identically - the source file has to change."""
    from app.social.errors import PermanentError
    from app.social.providers.meta_common import MetaGraphError, map_meta_error

    mapped = map_meta_error(
        MetaGraphError({"code": code, "message": "Video does not fit"}, 400))
    assert isinstance(mapped, PermanentError)
    assert "does not fit" in str(mapped)


# --------------------------------------------------------------------------
# Media is locked once submitted for approval
# --------------------------------------------------------------------------

def _compose(client, post_id=None, **fields):
    form = {"title": "t", "post_type": "image", "caption": "hello"}
    form.update(fields)
    url = f"/social/posts/{post_id}" if post_id else "/social/posts"
    return client.post(url, data=form, follow_redirects=True)


def test_changing_media_on_a_submitted_post_returns_it_to_draft(
        session, client, make_user, login):
    """Otherwise a reviewer approves content they never saw: submit one
    file, swap it, and the approval still stands."""
    from app.extensions import db
    from app.models import SocialPost, SocialPostTarget, SocialMediaAsset

    actor = make_user("admin", permissions=["manage_social"])
    login(actor)

    post = SocialPost(title="lock me", status="pending_approval")
    db.session.add(post)
    db.session.flush()
    target = SocialPostTarget(social_post_id=post.id, platform="fake",
                              post_type="image", status="draft")
    db.session.add(target)
    db.session.flush()
    db.session.add(SocialMediaAsset(
        target_id=target.id, source="upload",
        object_key="social_uploads/original.jpg", role="main", sort_order=0))
    db.session.commit()
    post_id = post.id

    _compose(client, post_id,
             upload_media="social_uploads/SWAPPED.jpg::image/jpeg")

    assert db.session.get(SocialPost, post_id).status == "draft"


def test_editing_only_the_caption_leaves_approval_alone(
        session, client, make_user, login):
    """The lock is about media, not about typo fixes."""
    from app.extensions import db
    from app.models import SocialPost, SocialPostTarget, SocialMediaAsset

    actor = make_user("admin", permissions=["manage_social"])
    login(actor)

    post = SocialPost(title="keep me", status="pending_approval")
    db.session.add(post)
    db.session.flush()
    target = SocialPostTarget(social_post_id=post.id, platform="fake",
                              post_type="image", status="draft")
    db.session.add(target)
    db.session.flush()
    db.session.add(SocialMediaAsset(
        target_id=target.id, source="upload",
        object_key="social_uploads/original.jpg", role="main", sort_order=0))
    db.session.commit()
    post_id = post.id

    _compose(client, post_id, caption="fixed a typo",
             upload_media="social_uploads/original.jpg::image/jpeg")

    assert db.session.get(SocialPost, post_id).status == "pending_approval"


def test_one_post_fans_out_as_reel_on_both_platforms(
        session, client, make_user, login):
    """The reported scenario, end to end through the composer route.

    ONE post, ONE approved file, and both platforms get it - which is the
    whole point: this agency counts Facebook + Instagram as one post, so
    "drop Instagram and post it separately" was never an acceptable fix.
    """
    import json

    from app.extensions import db
    from app.models import SocialAccount, SocialPost

    actor = make_user("admin", permissions=["manage_social"])
    login(actor)

    ids = []
    for i, platform in enumerate(("facebook", "instagram"), 1):
        account = SocialAccount(
            platform=platform, external_id=f"FIT-{i}",
            display_name=f"{platform} channel", account_type="page",
            status="active")
        db.session.add(account)
        db.session.flush()
        ids.append(account.id)
    db.session.commit()

    value = "social_uploads/clip.mp4::video/mp4"
    resp = client.post("/social/posts", data={
        "title": "fan out", "post_type": "video", "caption": "hello",
        "upload_media": value,
        "account_ids": [str(i) for i in ids],
        "media_measurements": json.dumps({
            f"upload_media|{value}": {"width": 720, "height": 1280,
                                      "duration": 30}}),
    }, follow_redirects=True)
    assert resp.status_code == 200

    post = SocialPost.query.filter_by(title="fan out").first()
    assert post is not None, "the composer did not create the post"

    by_platform = {t.platform: t for t in post.targets}
    assert set(by_platform) == {"facebook", "instagram"}, \
        "both platforms must be on the SAME post"
    assert by_platform["facebook"].post_type == "reel"
    assert by_platform["instagram"].post_type == "reel", (
        "this is the bug: Instagram used to be sent post_type=video, which "
        "its capabilities reject, so it never published")

    # The measurement is stored, so the pre-flight at schedule time can
    # re-check without measuring again.
    asset = by_platform["instagram"].media[0]
    assert (asset.meta or {}).get("measurements", {}).get("duration") == 30
