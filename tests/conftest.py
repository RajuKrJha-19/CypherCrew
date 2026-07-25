"""Pytest fixtures for the Social Publishing Engine.

The engine is provider-agnostic, so the whole suite runs against a
FakeProvider (registered as platform "fake") - no network, no credentials,
no real platform. Tests exercise the queue/state-machine, retry engine,
rate gate, scheduler, recovery and status exactly as a real adapter would
drive them.

Isolation: the engine owns dedicated tables (social_*, publish_*, etc.),
so each test truncates only those tables before and after it runs. No
business-domain table is ever touched.

The env vars below must be set BEFORE `app` is imported (config reads them
at import time), which is why they're at module top.
"""

import os
from datetime import datetime, timedelta

from cryptography.fernet import Fernet

os.environ.setdefault("SOCIAL_ENGINE_ENABLED", "true")
os.environ.setdefault("SOCIAL_TOKEN_KEY", Fernet.generate_key().decode())
os.environ.setdefault("SOCIAL_WORKER_TOKEN", "test-worker-token")
os.environ.setdefault("AUTO_SEED", "false")

import pytest  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db as _db  # noqa: E402
from app.social.registry import registry  # noqa: E402
from app.social.providers.base import SocialProvider  # noqa: E402
from app.social.dto import (  # noqa: E402
    AccountInfo, Capabilities, PublishStep, TokenBundle,
)
from app.social.errors import (  # noqa: E402
    AuthError, PermanentError, TransientError,
)


class FakeProvider(SocialProvider):
    """A fully in-memory provider. `mode` (class attr, reset to 'ok' per
    test) selects the behaviour of start_publish:
      ok        -> DONE
      pending   -> PENDING (poll_publish then returns DONE)
      transient -> raises, mapped to TransientError
      auth      -> raises, mapped to AuthError
      permanent -> raises, mapped to PermanentError
    """

    key = "fake"
    capabilities = Capabilities(
        post_types={"image", "carousel", "video"},
        publish_rate=(100, "24h"),
        max_carousel=10,
    )
    mode = "ok"

    def build_oauth_url(self, state, redirect_uri):
        return f"https://fake.test/auth?state={state}"

    def exchange_code(self, code, code_verifier, redirect_uri):
        return TokenBundle(access_token="AT", scopes="fake_publish")

    def list_publishable_accounts(self, token):
        return [AccountInfo("EXT1", "Fake Page", "page")]

    def validate(self, content):
        return []

    def start_publish(self, target, content, token):
        m = FakeProvider.mode
        if m == "transient":
            raise RuntimeError("transient boom")
        if m == "auth":
            raise RuntimeError("401 invalid token")
        if m == "permanent":
            raise RuntimeError("400 bad request")
        if m == "pending":
            return PublishStep(status="pending", provider_state={"container": "C1"})
        return PublishStep(status="done", external_post_id="EXT_POST_1",
                           permalink="https://fake.test/p/1")

    def poll_publish(self, target, provider_state, token):
        return PublishStep(status="done", external_post_id="EXT_POST_1",
                           permalink="https://fake.test/p/1")

    def fetch_analytics(self, target, token):
        return {"likes": 3, "reach": 10}

    def map_error(self, exc):
        m = FakeProvider.mode
        s = str(exc)
        if m == "transient":
            return TransientError(s)
        if m == "auth":
            return AuthError(s)
        return PermanentError(s)


def _social_models():
    from app.models import (
        PublishResult, PublishJob, SocialMediaAsset, SocialAnalyticsSnapshot,
        PlatformRateBudget, SocialAuditLog, ContentVersion, SocialPostTarget,
        SocialPost, SocialOAuthState, SocialAccount,
    )
    return [
        PublishResult, PublishJob, SocialMediaAsset, SocialAnalyticsSnapshot,
        PlatformRateBudget, SocialAuditLog, ContentVersion, SocialPostTarget,
        SocialPost, SocialOAuthState, SocialAccount,
    ]


def _clean():
    for model in _social_models():
        _db.session.query(model).delete()
    _db.session.commit()


@pytest.fixture(scope="session")
def app():
    application = create_app()
    if registry.get("fake") is None:
        registry.register(FakeProvider())
    yield application


@pytest.fixture()
def session(app):
    with app.app_context():
        FakeProvider.mode = "ok"
        _clean()
        yield _db.session
        _db.session.rollback()
        _clean()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def make_target(session):
    """Factory: build an approved, scheduled (due) target on the fake
    platform, returning (account, post, target)."""
    def _make(platform="fake", account_status="active", when_past=True,
              post_status="approved", post_type="image"):
        from app.models import (
            SocialAccount, SocialPost, SocialPostTarget, SocialMediaAsset,
        )
        from app.social.tokens.vault import get_vault

        acct = SocialAccount(
            platform=platform, external_id="EXT1", display_name="Fake Page",
            account_type="page", status=account_status,
            token_ciphertext=get_vault().encrypt("AT"), token_key_version=1,
        )
        session.add(acct)
        session.flush()

        post = SocialPost(title="t", base_caption="c", status=post_status,
                          approved_at=datetime.utcnow())
        session.add(post)
        session.flush()

        when = (datetime.utcnow() - timedelta(minutes=1)) if when_past \
            else (datetime.utcnow() + timedelta(hours=1))
        target = SocialPostTarget(
            social_post_id=post.id, social_account_id=acct.id,
            platform=platform, post_type=post_type, caption="hi",
            status="scheduled", scheduled_for=when,
        )
        session.add(target)
        session.flush()
        session.add(SocialMediaAsset(
            target_id=target.id, source="upload", object_key="x.jpg",
            role="main",
        ))
        session.commit()
        return acct, post, target

    return _make
