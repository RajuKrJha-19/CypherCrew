"""The auto-posted first comment.

It runs after the publish, on a code path that must never fail the
publish - which is exactly how it managed to be broken without anyone
noticing. These tests pin both halves: that it actually posts where the
platform allows it, and that every way it can NOT post leaves a trace
somebody can read.
"""

import pytest

from app.extensions import db
from app.models import SocialAuditLog
from app.social.dto import Capabilities, PublishStep, StepStatus
from app.social.providers.simulation import SimulationProvider
from app.social.queue import worker
from tests.conftest import FakeProvider


class _NoMethodProvider:
    """A provider that DECLARES the first-comment capability but never
    implemented post_first_comment - the exact adapter gap the worker must
    record rather than silently swallow. FakeProvider can't stand in here
    because it implements the method."""
    capabilities = Capabilities(
        post_types={"image"}, supports_first_comment=True)
    # deliberately no post_first_comment


def _step(external_post_id="EXT-1"):
    return PublishStep(status=StepStatus.DONE.value,
                       external_post_id=external_post_id,
                       permalink="https://example.invalid/p/1")


def _actions(target_id):
    return [
        row.action for row in
        SocialAuditLog.query
        .filter(SocialAuditLog.target_id == target_id,
                SocialAuditLog.action.like("first_comment%"))
        .order_by(SocialAuditLog.id)
        .all()
    ]


def _detail(target_id):
    row = (SocialAuditLog.query
           .filter(SocialAuditLog.target_id == target_id,
                   SocialAuditLog.action.like("first_comment%"))
           .order_by(SocialAuditLog.id.desc()).first())
    return row.detail or {}


# --------------------------------------------------------------------------
# The capability that was missing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("platform", ["instagram", "facebook", "linkedin",
                                      "youtube", "x"])
def test_simulated_channels_support_a_first_comment(platform):
    """Left unset, the worker skipped the step entirely and the composer
    greyed the box out - a demo channel accepted a first comment and threw
    it away without a word."""
    provider = SimulationProvider(platform)
    assert provider.capabilities.supports_first_comment is True
    assert hasattr(provider, "post_first_comment")


def test_google_business_still_has_no_first_comment():
    """Not a blanket True - GBP posts genuinely take no comments."""
    provider = SimulationProvider("google_business")
    assert provider.capabilities.supports_first_comment is False


def test_the_simulated_provider_returns_a_comment_id():
    provider = SimulationProvider("instagram")
    assert provider.post_first_comment("EXT-9", "hello", "tok") \
        == "SIM-COMMENT-EXT-9"


def test_the_simulated_provider_can_be_made_to_fail():
    from app.social.errors import PermanentError
    provider = SimulationProvider("instagram")
    with pytest.raises(PermanentError):
        provider.post_first_comment("EXT-9", "boom #simfail", "tok")


# --------------------------------------------------------------------------
# The Meta scopes it needs to write a comment at all
# --------------------------------------------------------------------------

def test_meta_asks_for_the_scopes_that_let_it_comment():
    """POST /{id}/comments is refused without these, and the refusal was
    swallowed - so the feature was permanently dead on real Meta."""
    from app.social.providers.meta_facebook import MetaFacebookProvider
    from app.social.providers.meta_instagram import MetaInstagramProvider

    assert "pages_manage_engagement" in MetaFacebookProvider.SCOPES
    assert "instagram_manage_comments" in MetaInstagramProvider.SCOPES


def test_meta_asks_for_the_scopes_that_let_it_read_insights():
    """Same failure mode as the comment scope - fetch_analytics returns {}
    on error, so a missing scope shows up as a permanently empty Analytics
    screen rather than as an error."""
    from app.social.providers.meta_facebook import MetaFacebookProvider
    from app.social.providers.meta_instagram import MetaInstagramProvider

    assert "read_insights" in MetaFacebookProvider.SCOPES
    assert "instagram_manage_insights" in MetaInstagramProvider.SCOPES


# --------------------------------------------------------------------------
# The happy path, end to end through the worker
# --------------------------------------------------------------------------

def test_a_first_comment_is_posted_and_recorded(session, make_target):
    _, _, target = make_target()
    target.first_comment = "  #hashtags in the first comment  "
    session.flush()

    provider = SimulationProvider("instagram")
    worker._post_first_comment(target, _step(), provider, "tok")
    db.session.flush()

    assert _actions(target.id) == ["first_comment_posted"]
    assert _detail(target.id)["comment_id"] == "SIM-COMMENT-EXT-1"


def test_no_first_comment_means_no_audit_noise(session, make_target):
    _, _, target = make_target()
    target.first_comment = None
    session.flush()

    worker._post_first_comment(target, _step(),
                               SimulationProvider("instagram"), "tok")
    db.session.flush()
    assert _actions(target.id) == []


# --------------------------------------------------------------------------
# Every way it can fail now says so
# --------------------------------------------------------------------------

def test_a_failure_is_recorded_rather_than_only_logged(session, make_target):
    _, _, target = make_target()
    target.first_comment = "this one blows up #simfail"
    session.flush()

    worker._post_first_comment(target, _step(),
                               SimulationProvider("instagram"), "tok")
    db.session.flush()

    assert _actions(target.id) == ["first_comment_failed"]
    assert "simfail" in _detail(target.id)["error"].lower()


def test_a_publish_still_succeeds_when_the_comment_fails(session, make_target):
    """The post is already live - the comment must never take it down."""
    _, _, target = make_target()
    target.first_comment = "#simfail"
    session.flush()

    worker._post_first_comment(target, _step(),
                               SimulationProvider("instagram"), "tok")


def test_an_unsupported_platform_is_recorded(session, make_target):
    _, _, target = make_target()
    target.first_comment = "hello"
    session.flush()

    worker._post_first_comment(target, _step(),
                               SimulationProvider("google_business"), "tok")
    db.session.flush()

    assert _actions(target.id) == ["first_comment_skipped"]
    assert "support" in _detail(target.id)["reason"]


def test_a_story_is_skipped_with_a_reason(session, make_target):
    _, _, target = make_target(post_type="story")
    target.first_comment = "hello"
    session.flush()

    worker._post_first_comment(target, _step(),
                               SimulationProvider("instagram"), "tok")
    db.session.flush()

    assert _actions(target.id) == ["first_comment_skipped"]
    assert "stories" in _detail(target.id)["reason"]


def test_a_missing_post_id_is_recorded(session, make_target):
    _, _, target = make_target()
    target.first_comment = "hello"
    session.flush()

    worker._post_first_comment(target, _step(external_post_id=None),
                               SimulationProvider("instagram"), "tok")
    db.session.flush()

    assert _actions(target.id) == ["first_comment_skipped"]
    assert "post id" in _detail(target.id)["reason"]


def test_an_adapter_without_the_method_is_recorded(session, make_target):
    """A provider that declares the capability but has no post_first_comment -
    the case that silently did nothing - now leaves an audit trace."""
    _, _, target = make_target()
    target.first_comment = "hello"
    session.flush()

    provider = _NoMethodProvider()
    assert not hasattr(provider, "post_first_comment")

    worker._post_first_comment(target, _step(), provider, "tok")
    db.session.flush()

    assert _actions(target.id) == ["first_comment_skipped"]
    assert "can't post comments" in _detail(target.id)["reason"]
