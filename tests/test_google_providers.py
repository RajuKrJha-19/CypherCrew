"""YouTube + Google Business Profile adapters.

The interesting logic is offline-testable without touching Google: the
resumable-upload state machine, the account/location name juggling that
Business Profile's split API forces on us, error classification, and the
one-hour-token refresh that Meta never needed.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import requests

from app.social.dto import MediaRef, PostContent
from app.social.errors import (
    AuthError, PermanentError, RateLimitError, TransientError,
)
from app.social.providers.google_business import GoogleBusinessProvider
from app.social.providers.google_common import (
    GoogleHTTPError, map_google_error,
)
from app.social.providers.youtube import YouTubeProvider


# --------------------------------------------------------------------------
# Error classification - decides whether a post retries or dies
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status,reason,expected", [
    (401, None, AuthError),
    (403, "insufficientPermissions", AuthError),
    (429, None, RateLimitError),
    (403, "quotaExceeded", RateLimitError),
    (500, None, TransientError),
    (503, None, TransientError),
    (400, None, PermanentError),
    (404, None, PermanentError),
])
def test_google_errors_are_classified(status, reason, expected):
    error = {"message": "boom"}
    if reason:
        error["errors"] = [{"reason": reason}]
    assert isinstance(map_google_error(GoogleHTTPError(error, status)),
                      expected)


def test_a_network_error_is_transient():
    """A dropped connection must never kill a scheduled post."""
    assert isinstance(
        map_google_error(requests.exceptions.ConnectionError("reset")),
        TransientError)


def test_exhausted_youtube_quota_retries_rather_than_dying():
    """The daily upload allowance is small - running out means try
    tomorrow, not abandon the post."""
    error = {"message": "The request cannot be completed because you have "
                        "exceeded your quota."}
    assert isinstance(map_google_error(GoogleHTTPError(error, 403)),
                      RateLimitError)


# --------------------------------------------------------------------------
# YouTube: caption -> title + description
# --------------------------------------------------------------------------

def test_the_first_line_becomes_the_title():
    content = PostContent(platform="youtube", post_type="video",
                          caption="Launch film\nThe full story below.",
                          hashtags="#launch")
    title, description = YouTubeProvider._title_and_description(content)
    assert title == "Launch film"
    assert "The full story below." in description
    assert "#launch" in description


def test_a_single_line_caption_is_all_title():
    content = PostContent(platform="youtube", post_type="video",
                          caption="Just a title")
    title, description = YouTubeProvider._title_and_description(content)
    assert title == "Just a title"
    assert description == ""


def test_youtube_rejects_a_post_with_no_video(app):
    provider = YouTubeProvider()
    with app.app_context():
        problems = provider.validate(PostContent(
            platform="youtube", post_type="video", caption="Title"))
    assert any("video" in p.lower() for p in problems)


def test_youtube_rejects_an_image(app):
    provider = YouTubeProvider()
    content = PostContent(
        platform="youtube", post_type="video", caption="Title",
        media=[MediaRef(object_key="k.jpg", mime_type="image/jpeg")])
    with app.app_context():
        problems = provider.validate(content)
    assert any("video" in p.lower() for p in problems)


def test_youtube_requires_a_title(app):
    provider = YouTubeProvider()
    content = PostContent(
        platform="youtube", post_type="video", caption="  ",
        media=[MediaRef(object_key="k.mp4", mime_type="video/mp4")])
    with app.app_context():
        problems = provider.validate(content)
    assert any("title" in p.lower() for p in problems)


# --------------------------------------------------------------------------
# YouTube: the resumable-upload state machine
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {}
        self.content = b"{}"

    def json(self):
        return self._payload


def test_a_fresh_session_reports_zero_bytes(monkeypatch):
    """308 with no Range header means Google has nothing yet."""
    monkeypatch.setattr(requests, "put", lambda *a, **k: _Resp(308))
    assert YouTubeProvider()._session_offset("https://s", 100, "tok") == 0


def test_a_partial_session_resumes_from_the_next_byte(monkeypatch):
    """Range is inclusive, so 0-999 means byte 1000 is next. An off-by-one
    here silently corrupts every resumed upload."""
    monkeypatch.setattr(
        requests, "put",
        lambda *a, **k: _Resp(308, headers={"Range": "bytes=0-999"}))
    assert YouTubeProvider()._session_offset("https://s", 5000, "tok") == 1000


def test_a_finished_session_reports_complete(monkeypatch):
    monkeypatch.setattr(requests, "put", lambda *a, **k: _Resp(201))
    assert YouTubeProvider()._session_offset("https://s", 100, "tok") is None


def test_an_expired_session_is_transient(monkeypatch):
    """404 means the session is gone - worth starting again, not a dead
    post."""
    monkeypatch.setattr(requests, "put", lambda *a, **k: _Resp(404))
    with pytest.raises(TransientError):
        YouTubeProvider()._session_offset("https://s", 100, "tok")


def test_an_interrupted_upload_stays_pending(monkeypatch, app):
    """The whole point of the resumable protocol: keep the session and
    carry on next cycle instead of re-uploading from zero."""
    provider = YouTubeProvider()
    state = {"session_uri": "https://s", "source_url": "https://r2",
             "size": 5000, "mime": "video/mp4"}

    monkeypatch.setattr(provider, "_session_offset", lambda *a, **k: 1000)
    monkeypatch.setattr(provider, "_upload_from", lambda *a, **k: None)

    with app.app_context():
        step = provider.poll_publish(SimpleNamespace(), state, "tok")

    assert step.status == "pending"
    assert step.provider_state["session_uri"] == "https://s"


def test_a_completed_upload_returns_the_video(monkeypatch, app):
    provider = YouTubeProvider()
    state = {"session_uri": "https://s", "source_url": "https://r2",
             "size": 5000, "mime": "video/mp4"}

    monkeypatch.setattr(provider, "_session_offset", lambda *a, **k: 0)
    monkeypatch.setattr(provider, "_upload_from", lambda *a, **k: "VID123")

    with app.app_context():
        step = provider.poll_publish(SimpleNamespace(), state, "tok")

    assert step.status == "done"
    assert step.external_post_id == "VID123"
    assert step.permalink == "https://www.youtube.com/watch?v=VID123"


def test_an_upload_that_finishes_with_no_id_is_retried(app):
    """Better to retry than to record a published post nobody can find."""
    with app.app_context():
        with pytest.raises(TransientError):
            YouTubeProvider()._done(None, "tok")


# --------------------------------------------------------------------------
# Google Business Profile: the split-API name juggling
# --------------------------------------------------------------------------

def test_a_location_channel_carries_the_v4_path(app, monkeypatch):
    """Discovery reads v1 (`locations/678`) but posting needs v4
    (`accounts/123/locations/678`). Losing the account prefix makes the
    channel unpostable, so it is composed at connect time."""
    from app.social.providers import google_business as gb

    def fake_get(self, path, token, params=None):
        if path == "accounts":
            return {"accounts": [{"name": "accounts/123"}]}
        return {"locations": [{"name": "locations/678", "title": "Bakery"}]}

    monkeypatch.setattr(gb.GoogleClient, "get", fake_get)

    with app.app_context():
        accounts = gb.GoogleBusinessProvider().list_publishable_accounts("tok")

    assert len(accounts) == 1
    assert accounts[0].external_id == "accounts/123/locations/678"
    assert accounts[0].display_name == "Bakery"
    assert accounts[0].meta["account_name"] == "accounts/123"


def test_a_channel_without_the_full_path_refuses_to_publish(app):
    """Rather than posting to a path Google will not understand."""
    provider = GoogleBusinessProvider()
    target = SimpleNamespace(account=SimpleNamespace(external_id="locations/678"))
    content = PostContent(platform="google_business", post_type="text",
                          caption="Open late today")

    with app.app_context():
        with pytest.raises(PermanentError):
            provider.start_publish(target, content, "tok")


def test_a_processing_post_stays_pending(app, monkeypatch):
    """PROCESSING means Google has it but it is not live - reporting it as
    published would be a lie."""
    from app.social.providers import google_business as gb

    monkeypatch.setattr(
        gb.GoogleClient, "post",
        lambda self, path, token, json=None, params=None: {
            "name": "accounts/1/locations/2/localPosts/3",
            "state": "PROCESSING"})

    target = SimpleNamespace(
        account=SimpleNamespace(external_id="accounts/1/locations/2"))
    content = PostContent(platform="google_business", post_type="text",
                          caption="Open late today")

    with app.app_context():
        step = gb.GoogleBusinessProvider().start_publish(target, content, "tok")

    assert step.status == "pending"
    assert step.provider_state["name"].endswith("/localPosts/3")


def test_a_rejected_post_fails_permanently(app, monkeypatch):
    """Google rejected the content - retrying cannot help."""
    from app.social.providers import google_business as gb

    monkeypatch.setattr(
        gb.GoogleClient, "get",
        lambda self, path, token, params=None: {"state": "REJECTED"})

    with app.app_context():
        with pytest.raises(PermanentError):
            gb.GoogleBusinessProvider().poll_publish(
                SimpleNamespace(), {"name": "accounts/1/locations/2/localPosts/3"},
                "tok")


def test_a_live_post_returns_its_search_url(app, monkeypatch):
    from app.social.providers import google_business as gb

    monkeypatch.setattr(
        gb.GoogleClient, "get",
        lambda self, path, token, params=None: {
            "name": "accounts/1/locations/2/localPosts/3",
            "state": "LIVE", "searchUrl": "https://g.co/post"})

    with app.app_context():
        step = gb.GoogleBusinessProvider().poll_publish(
            SimpleNamespace(), {"name": "accounts/1/locations/2/localPosts/3"},
            "tok")

    assert step.status == "done"
    assert step.permalink == "https://g.co/post"


def test_business_profile_caps_the_summary(app):
    provider = GoogleBusinessProvider()
    content = PostContent(platform="google_business", post_type="text",
                          caption="x" * 3000)
    assert len(provider._summary(content)) == 1500


def test_business_profile_rejects_multiple_images(app):
    provider = GoogleBusinessProvider()
    content = PostContent(
        platform="google_business", post_type="image", caption="Hello",
        media=[MediaRef(object_key="a.jpg", mime_type="image/jpeg"),
               MediaRef(object_key="b.jpg", mime_type="image/jpeg")])
    with app.app_context():
        problems = provider.validate(content)
    assert any("one image" in p for p in problems)


# --------------------------------------------------------------------------
# The one-hour token, which Meta never had to deal with
# --------------------------------------------------------------------------

def test_a_token_about_to_expire_is_refreshed_before_use(session, monkeypatch):
    """A post scheduled for later would otherwise publish with a token that
    died hours ago."""
    from app.models import SocialAccount
    from app.social.services.accounts import AccountManager
    from app.social.tokens.vault import get_vault
    from app.social.dto import TokenBundle

    vault = get_vault()
    account = SocialAccount(
        platform="fake", external_id="G1", display_name="Chan",
        account_type="channel", status="active",
        token_ciphertext=vault.encrypt("OLD"), token_key_version=1,
        token_expires_at=datetime.utcnow() + timedelta(seconds=30),
    )
    session.add(account)
    session.flush()

    from tests.conftest import FakeProvider
    monkeypatch.setattr(
        FakeProvider, "refresh_token",
        lambda self, acct: TokenBundle(
            access_token="NEW",
            token_expires_at=datetime.utcnow() + timedelta(hours=1)),
        raising=False)

    assert AccountManager.access_token(account) == "NEW"


def test_a_healthy_token_is_not_refreshed(session, monkeypatch):
    from app.models import SocialAccount
    from app.social.services.accounts import AccountManager
    from app.social.tokens.vault import get_vault

    vault = get_vault()
    account = SocialAccount(
        platform="fake", external_id="G2", display_name="Chan",
        account_type="channel", status="active",
        token_ciphertext=vault.encrypt("STILL-GOOD"), token_key_version=1,
        token_expires_at=datetime.utcnow() + timedelta(hours=5),
    )
    session.add(account)
    session.flush()

    called = []
    from tests.conftest import FakeProvider
    monkeypatch.setattr(FakeProvider, "refresh_token",
                        lambda self, acct: called.append(1), raising=False)

    assert AccountManager.access_token(account) == "STILL-GOOD"
    assert not called


def test_a_failed_refresh_does_not_break_the_publish(session, monkeypatch):
    """The stored token may still have minutes on it - let the platform
    give a real error rather than crashing in the token layer."""
    from app.models import SocialAccount
    from app.social.services.accounts import AccountManager
    from app.social.tokens.vault import get_vault

    vault = get_vault()
    account = SocialAccount(
        platform="fake", external_id="G3", display_name="Chan",
        account_type="channel", status="active",
        token_ciphertext=vault.encrypt("OLD"), token_key_version=1,
        token_expires_at=datetime.utcnow() + timedelta(seconds=10),
    )
    session.add(account)
    session.flush()

    def boom(self, acct):
        raise RuntimeError("google is down")

    from tests.conftest import FakeProvider
    monkeypatch.setattr(FakeProvider, "refresh_token", boom, raising=False)

    assert AccountManager.access_token(account) == "OLD"


def test_meta_accounts_are_untouched_by_the_refresh_guard(session, monkeypatch):
    """Page tokens carry no expiry, so nothing extra should be called."""
    from app.models import SocialAccount
    from app.social.services.accounts import AccountManager
    from app.social.tokens.vault import get_vault

    vault = get_vault()
    account = SocialAccount(
        platform="fake", external_id="M1", display_name="Page",
        account_type="page", status="active",
        token_ciphertext=vault.encrypt("PAGE-TOKEN"), token_key_version=1,
        token_expires_at=None,
    )
    session.add(account)
    session.flush()

    called = []
    from tests.conftest import FakeProvider
    monkeypatch.setattr(FakeProvider, "refresh_token",
                        lambda self, acct: called.append(1), raising=False)

    assert AccountManager.access_token(account) == "PAGE-TOKEN"
    assert not called
