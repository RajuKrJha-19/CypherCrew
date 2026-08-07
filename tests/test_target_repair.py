"""Repairing one channel of a partially-published post.

A carousel went live on Facebook and Instagram refused it: the image was
1516x689, outside 4:5..1.91:1 and wider than 1440px. Three things were wrong
with what the screen then offered.

"Fix automatically" appeared on every target, but it only re-decides the POST
TYPE - so on a file that is simply the wrong shape it did nothing and said so
only after the click. Pressing it again did the same.

The failure read as two faults on two files: "Carousel item 1 can't publish
here: <aspect>. Carousel item 1 can't publish here: <width>." One item, one
sentence.

And there was no way to fix it at all without deleting the post or
duplicating it, because a post with a live sibling correctly refuses a
whole-post edit.
"""
import pytest

from app.extensions import db
from app.models import SocialMediaAsset
from app.social.media import fit


# ======================================================================
# The message: one sentence per item, whatever the count
# ======================================================================

ASPECT = "it is 1516x689 and this needs between 4:5 and 1.91:1"
WIDTH = "it is 1516px wide and the maximum is 1440px"


def test_two_reasons_about_one_file_read_as_one_sentence():
    joined = fit.join_reasons([ASPECT, WIDTH])
    assert joined == f"{ASPECT}; {WIDTH}"
    # The actual complaint: the prefix is not repeated.
    assert joined.count("it is 1516") == 2      # both facts kept
    assert "can't publish here" not in joined   # prefix belongs to the caller


def test_one_reason_is_left_exactly_as_it_was():
    assert fit.join_reasons([ASPECT]) == ASPECT


def test_three_reasons_stay_grammatical():
    """An earlier version folded the subject out and produced "and 40s and the
    maximum is 30s"."""
    third = "it is 40s and the maximum is 30s"
    out = fit.join_reasons([ASPECT, WIDTH, third])
    assert out.endswith(third)
    for part in (ASPECT, WIDTH, third):
        assert part in out


def test_no_reasons_is_empty():
    assert fit.join_reasons([]) == ""
    assert fit.join_reasons(None) == ""


def test_the_carousel_check_emits_one_problem_per_item(app):
    """End to end through the provider, not just the helper."""
    from app.social.dto import PostContent
    from app.social.providers.meta_instagram import MetaInstagramProvider

    class _M:
        mime_type = "image/jpeg"
        measurements = {"width": 1516, "height": 689}

    content = PostContent(platform="instagram", post_type="carousel",
                          caption="x", media=[_M(), _M()])
    with app.app_context():
        problems = MetaInstagramProvider().validate(content)

    carousel = [p for p in problems if p.startswith("Carousel item")]
    # Two items, two sentences - not four.
    assert len(carousel) == 2, carousel
    assert carousel[0].startswith("Carousel item 1 can't publish here: ")
    assert carousel[0].count("can't publish here") == 1
    # Both facts still reach the reader.
    assert "1.91:1" in carousel[0] and "1440px" in carousel[0]


# ======================================================================
# "Fix automatically" is only offered when it can help
# ======================================================================

def _target(session, make_target, status="failed", post_type="carousel"):
    _acct, post, target = make_target()
    target.status = status
    target.post_type = post_type
    session.commit()
    return post, target


def test_no_remap_offered_when_the_file_itself_is_wrong(app, session,
                                                        make_target,
                                                        monkeypatch):
    from app.routes import social as social_routes

    _post, target = _target(session, make_target)
    # choose_post_type answering None = "the file has to change".
    monkeypatch.setattr(social_routes.media_fit, "choose_post_type",
                        lambda *a, **k: (None, ["too wide"]))
    with app.test_request_context():
        assert social_routes._target_repairs(target)["remap"] is False


def test_no_remap_offered_when_it_would_choose_the_same_type(app, session,
                                                            make_target,
                                                            monkeypatch):
    from app.routes import social as social_routes

    _post, target = _target(session, make_target, post_type="carousel")
    monkeypatch.setattr(social_routes.media_fit, "choose_post_type",
                        lambda *a, **k: ("carousel", []))
    with app.test_request_context():
        assert social_routes._target_repairs(target)["remap"] is False


def test_remap_is_offered_when_it_would_actually_change_the_type(
        app, session, make_target, monkeypatch):
    from app.routes import social as social_routes

    _post, target = _target(session, make_target, post_type="video")
    monkeypatch.setattr(social_routes.media_fit, "choose_post_type",
                        lambda *a, **k: ("reel", []))
    with app.test_request_context():
        assert social_routes._target_repairs(target)["remap"] is True


def test_a_published_target_offers_no_repairs_at_all(app, session,
                                                     make_target):
    from app.routes import social as social_routes

    _post, target = _target(session, make_target, status="published")
    with app.test_request_context():
        out = social_routes._target_repairs(target)
    assert out == {"remap": False, "crop": [], "reauth": False}


# ======================================================================
# Replacing one channel's image, in place
# ======================================================================

def _asset(session, target, key="social_uploads/abc_orig.jpg"):
    a = SocialMediaAsset(social_post_id=target.social_post_id,
                         target_id=target.id, source="upload",
                         object_key=key, mime_type="image/jpeg", role="main",
                         meta={"measurements": {"width": 1516, "height": 689}})
    session.add(a)
    session.commit()
    return a


def test_a_failed_targets_image_can_be_swapped(app, session, client,
                                               make_user, login, make_target):
    login(make_user("admin", permissions=["manage_social"]))
    _post, target = _target(session, make_target)
    asset = _asset(session, target)

    resp = client.post(f"/social/media/{asset.id}/replace",
                       data={"object_key": "social_uploads/def_crop.jpg"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True

    db.session.refresh(asset)
    assert asset.object_key == "social_uploads/def_crop.jpg"
    # The old numbers must not judge the new file.
    assert not (asset.meta or {}).get("measurements")
    db.session.refresh(target)
    assert target.last_error is None


def test_a_published_channel_is_never_rewritten(app, session, client,
                                                make_user, login, make_target):
    """The whole point is that the live sibling is untouched."""
    login(make_user("admin", permissions=["manage_social"]))
    _post, target = _target(session, make_target, status="published")
    asset = _asset(session, target)

    resp = client.post(f"/social/media/{asset.id}/replace",
                       data={"object_key": "social_uploads/def_crop.jpg"})
    assert resp.status_code == 409
    db.session.refresh(asset)
    assert asset.object_key == "social_uploads/abc_orig.jpg"


def test_an_unreachable_key_is_refused(app, session, client, make_user,
                                       login, make_target):
    """Same IDOR guard as the crop and AI paths - you cannot point a post at a
    file you were never allowed to see."""
    login(make_user("admin", permissions=["manage_social"]))
    _post, target = _target(session, make_target)
    asset = _asset(session, target)

    resp = client.post(f"/social/media/{asset.id}/replace",
                       data={"object_key": "some/other/place/secret.jpg"})
    assert resp.status_code == 403
    db.session.refresh(asset)
    assert asset.object_key == "social_uploads/abc_orig.jpg"


# ======================================================================
# An expired token reads as "reconnect", not as Google's developer docs
# ======================================================================

GOOGLE_401 = ("Request had invalid authentication credentials. Expected OAuth "
              "2 access token, login cookie or other valid authentication "
              "credential. See https://developers.google.com/identity/"
              "sign-in/web/devconsole-project.")


def _failed_with_token(session, make_target, account_status):
    _acct, post, target = make_target()
    target.status = "failed"
    target.last_error = GOOGLE_401
    target.account.status = account_status
    session.commit()
    return post, target


def test_a_channel_needing_reauth_is_reported_as_such(app, session,
                                                      make_target):
    from app.routes import social as social_routes

    _post, target = _failed_with_token(session, make_target, "needs_reauth")
    with app.test_request_context():
        assert social_routes._target_repairs(target)["reauth"] is True


def test_a_healthy_channel_is_not_reported_as_needing_reauth(app, session,
                                                             make_target):
    from app.routes import social as social_routes

    _post, target = _failed_with_token(session, make_target, "active")
    with app.test_request_context():
        assert social_routes._target_repairs(target)["reauth"] is False


def test_the_page_says_reconnect_instead_of_quoting_the_api(
        app, session, client, make_user, login, make_target):
    """The whole complaint: a social manager was shown Google's wording, a
    developer-console URL, and no action."""
    login(make_user("admin", permissions=["manage_social"]))
    post, _target = _failed_with_token(session, make_target, "needs_reauth")

    body = client.get(f"/social/posts/{post.id}").get_data(as_text=True)

    assert "connection has expired" in body
    assert "Reconnect" in body
    assert "developers.google.com" not in body
    assert "invalid authentication credentials" not in body


def test_a_content_failure_still_shows_the_real_reason(
        app, session, client, make_user, login, make_target):
    """Only the token case is rewritten - a genuine content problem must keep
    saying what is actually wrong with the file."""
    login(make_user("admin", permissions=["manage_social"]))
    post, target = _failed_with_token(session, make_target, "active")
    target.last_error = "Carousel item 1 can't publish here: it is too wide."
    session.commit()

    body = client.get(f"/social/posts/{post.id}").get_data(as_text=True)
    assert "it is too wide" in body
    assert "connection has expired" not in body
