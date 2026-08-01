"""AI Settings (Phase 3): per-task provider/model resolution, the soft on/off
toggle, and the admin screen (render + save + gates). Simulation stays on, so
nothing calls a real provider.
"""
import pytest

from app.ai import settings as ai_settings
from app.extensions import db
from app.models import AISettings


@pytest.fixture(autouse=True)
def _clean_ai_settings(app):
    """The AISettings row is a single global row (not test-prefixed), so wipe
    it around every test to keep resolution deterministic."""
    def _wipe():
        with app.app_context():
            AISettings.query.delete()
            db.session.commit()
    _wipe()
    yield
    _wipe()


# -- Resolution + toggle ----------------------------------------------------

def test_resolve_falls_back_to_env_defaults(app):
    with app.test_request_context():
        assert ai_settings.resolve("caption") == (
            app.config["AI_CAPTION_PROVIDER"], app.config["AI_CAPTION_MODEL"])
        assert ai_settings.resolve("qa") == (
            app.config["AI_QA_PROVIDER"], app.config["AI_QA_MODEL"])


def test_resolve_uses_db_override(app):
    with app.app_context():
        db.session.add(AISettings(
            caption_provider="openai", caption_model="gpt-5-mini",
            qa_provider="gemini", qa_model="gemini-2.5-pro"))
        db.session.commit()
    with app.test_request_context():
        assert ai_settings.resolve("caption") == ("openai", "gpt-5-mini")
        assert ai_settings.resolve("qa") == ("gemini", "gemini-2.5-pro")


def test_resolve_provider_override_uses_that_providers_model(app):
    # Provider switched to OpenAI but model left blank -> must use OpenAI's own
    # default model, never the (Gemini) env AI_CAPTION_MODEL default.
    with app.app_context():
        db.session.add(AISettings(caption_provider="openai"))
        db.session.commit()
    with app.test_request_context():
        provider, model = ai_settings.resolve("caption")
        assert provider == "openai"
        assert model == "gpt-5-mini"
        assert model != app.config["AI_CAPTION_MODEL"]


def test_is_enabled_soft_toggle(app):
    with app.test_request_context():
        assert ai_settings.is_enabled() is True          # env on, no row
    with app.app_context():
        db.session.add(AISettings(enabled=False))
        db.session.commit()
    with app.test_request_context():
        assert ai_settings.is_enabled() is False          # admin paused it


def test_key_present_reflects_config(app):
    with app.test_request_context():
        app.config["GEMINI_API_KEY"] = "test-key"
        try:
            assert ai_settings.key_present("gemini") is True
        finally:
            app.config["GEMINI_API_KEY"] = None
        assert ai_settings.key_present("openai") is False


# -- Admin screen -----------------------------------------------------------

def test_settings_screen_renders_for_admin(client, login, make_user):
    login(make_user("admin"))
    r = client.get("/admin/ai/")
    assert r.status_code == 200
    assert b"AI Settings" in r.data


def test_settings_screen_forbidden_for_non_admin(client, login, make_user):
    login(make_user("employee"))
    r = client.get("/admin/ai/")
    assert r.status_code == 403


def test_settings_save_persists_choices(client, login, make_user, app):
    login(make_user("admin"))
    r = client.post("/admin/ai/", data={
        "enabled": "on",
        "caption_provider": "openai", "caption_model": "gpt-5-mini",
        "qa_provider": "gemini", "qa_model": "gemini-2.5-pro",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    with app.app_context():
        row = AISettings.query.first()
        assert row.enabled is True
        assert row.caption_provider == "openai"
        assert row.caption_model == "gpt-5-mini"
        assert row.qa_provider == "gemini"


def test_settings_save_rejects_unknown_provider(client, login, make_user, app):
    login(make_user("admin"))
    client.post("/admin/ai/", data={
        "enabled": "on", "caption_provider": "bogus", "caption_model": "x"},
        follow_redirects=False)
    with app.app_context():
        row = AISettings.query.first()
        # Unknown provider is dropped (falls back to env default), not stored.
        assert row.caption_provider is None


# -- Soft toggle gates the live routes --------------------------------------

def test_soft_disable_blocks_the_caption_route(
        client, login, make_user, make_task, app):
    user = make_user("employee", permissions=["manage_social"])
    task = make_task(user)
    with app.app_context():
        db.session.add(AISettings(enabled=False))
        db.session.commit()
    login(user)
    r = client.post("/social/api/ai/caption", data={"task_id": task.id})
    assert r.status_code == 503


# -- OpenAI adapter is import-safe + fails cleanly without a key ------------

def test_openai_adapter_import_safe_and_keyless_raises_auth():
    from app.ai.base import CaptionContext
    from app.ai.errors import AIAuth
    from app.ai.providers.openai import OpenAIProvider

    prov = OpenAIProvider(model="gpt-5-mini", api_key=None)
    with pytest.raises(AIAuth):
        prov.generate_caption(CaptionContext(brief="hi", platforms=["twitter"]))
