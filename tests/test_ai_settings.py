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


def test_resolve_reply_falls_back_to_caption_defaults(app):
    # No override -> review replies ride the caption model (short text, cheap).
    with app.test_request_context():
        assert ai_settings.resolve("reply") == (
            app.config["AI_CAPTION_PROVIDER"], app.config["AI_CAPTION_MODEL"])


def test_resolve_reply_uses_own_override_independent_of_caption(app):
    # Replies post publicly - an admin can put them on a different model without
    # touching captions.
    with app.app_context():
        db.session.add(AISettings(
            caption_provider="gemini", caption_model="gemini-2.5-flash",
            reply_provider="openai", reply_model="gpt-5"))
        db.session.commit()
    with app.test_request_context():
        assert ai_settings.resolve("caption") == ("gemini", "gemini-2.5-flash")
        assert ai_settings.resolve("reply") == ("openai", "gpt-5")


def test_is_enabled_soft_toggle(app):
    with app.test_request_context():
        assert ai_settings.is_enabled() is True          # env on, no row
    with app.app_context():
        db.session.add(AISettings(enabled=False))
        db.session.commit()
    with app.test_request_context():
        assert ai_settings.is_enabled() is False          # admin paused it


def test_feature_enabled_defaults_on(app):
    with app.test_request_context():
        for f in ("caption", "qa", "reply", "comment"):
            assert ai_settings.feature_enabled(f) is True


def test_feature_enabled_master_off_disables_every_feature(app):
    with app.app_context():
        db.session.add(AISettings(enabled=False))
        db.session.commit()
    with app.test_request_context():
        for f in ("caption", "qa", "reply", "comment"):
            assert ai_settings.feature_enabled(f) is False


def test_feature_enabled_individual_switch(app):
    # Master on, but Media QA turned off individually.
    with app.app_context():
        db.session.add(AISettings(enabled=True, qa_enabled=False))
        db.session.commit()
    with app.test_request_context():
        assert ai_settings.feature_enabled("caption") is True
        assert ai_settings.feature_enabled("qa") is False
        states = ai_settings.feature_states()
        assert states["caption"] is True and states["qa"] is False


# -- control panel: caption prefs / performance / auto-reply guardrails -----

def test_caption_prefs_defaults_and_override(app):
    with app.test_request_context():
        assert ai_settings.caption_prefs() == {
            "tone": None, "variations": 2, "hashtags": True}
    with app.app_context():
        db.session.add(AISettings(caption_tone="Punchy", caption_variations=1,
                                  caption_hashtags=False))
        db.session.commit()
    with app.test_request_context():
        p = ai_settings.caption_prefs()
        assert p["tone"] == "punchy" and p["variations"] == 1
        assert p["hashtags"] is False


def test_caption_prompt_respects_variations_and_hashtags():
    from app.ai import prompts
    from app.ai.base import CaptionContext
    sys_off, _ = prompts.caption_prompt(CaptionContext(
        brief="x", variations=0, hashtags=False))
    assert "'variations' must be an empty array" in sys_off
    assert "must be an empty array - do NOT add any hashtags" in sys_off
    sys_on, _ = prompts.caption_prompt(CaptionContext(
        brief="x", variations=3, hashtags=True))
    assert "3 alternative full captions" in sys_on


def test_autoreply_config_env_fallback_then_db_override(app):
    with app.test_request_context():
        app.config["GBP_AUTOREPLY_ENABLED"] = True
        try:
            c = ai_settings.autoreply_config()
        finally:
            app.config["GBP_AUTOREPLY_ENABLED"] = False
        assert c["enabled"] is True and c["min_rating"] == 4
    with app.app_context():
        db.session.add(AISettings(gbp_autoreply_enabled=True, gbp_min_rating=5,
                                  gbp_blocklist="refund, legal"))
        db.session.commit()
    with app.test_request_context():
        c = ai_settings.autoreply_config()
        assert c["enabled"] is True and c["min_rating"] == 5
        assert "refund" in c["blocklist"] and "legal" in c["blocklist"]


def test_save_persists_caption_and_performance(client, login, make_user, app):
    login(make_user("admin"))
    client.post("/admin/ai/", data={
        "enabled": "on", "caption_tone": "festive", "caption_variations": "3",
        "caption_hashtags": "on", "image_max_dim": "1024", "media_max_mb": "8"})
    with app.app_context():
        row = AISettings.query.first()
        assert row.caption_tone == "festive" and row.caption_variations == 3
        assert row.caption_hashtags is True
        assert row.image_max_dim == 1024 and row.media_max_mb == 8


def test_save_clamps_and_enforces_floors(client, login, make_user, app):
    login(make_user("admin"))
    client.post("/admin/ai/", data={
        "enabled": "on", "caption_variations": "9",     # -> clamped to 3
        "gbp_min_rating": "1",                          # -> floored to 3
        "gbp_blocklist": "refund", "gbp_autoreply_enabled": "on"})
    with app.app_context():
        row = AISettings.query.first()
        assert row.caption_variations == 3
        assert row.gbp_min_rating == 3


def test_autosend_refused_without_blocklist(client, login, make_user, app):
    login(make_user("admin"))
    app.config["GBP_AUTOREPLY_BLOCKLIST"] = ""          # no env net either
    try:
        r = client.post("/admin/ai/", data={
            "enabled": "on", "gbp_autoreply_enabled": "on",
            "gbp_blocklist": ""}, follow_redirects=True)
    finally:
        app.config["GBP_AUTOREPLY_BLOCKLIST"] = "refund"
    with app.app_context():
        assert AISettings.query.first().gbp_autoreply_enabled is False
    assert b"blocklist" in r.data.lower()


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
        "reply_provider": "openai", "reply_model": "gpt-5",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    with app.app_context():
        row = AISettings.query.first()
        assert row.enabled is True
        assert row.caption_provider == "openai"
        assert row.caption_model == "gpt-5-mini"
        assert row.qa_provider == "gemini"
        assert row.reply_provider == "openai"
        assert row.reply_model == "gpt-5"


def test_settings_save_persists_feature_toggles(client, login, make_user, app):
    login(make_user("admin"))
    # Check master + caption + reply; leave qa + comment unchecked (= off).
    r = client.post("/admin/ai/", data={
        "enabled": "on", "feature_caption": "on", "feature_reply": "on",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    with app.app_context():
        row = AISettings.query.first()
        assert row.caption_enabled is True
        assert row.reply_enabled is True
        assert row.qa_enabled is False           # unchecked -> off
        assert row.comment_enabled is False


def test_caption_feature_off_blocks_the_route(
        client, login, make_user, make_task, app):
    # Master ON, captions turned off individually -> the caption route refuses.
    user = make_user("employee", permissions=["manage_social"])
    task = make_task(user)
    with app.app_context():
        db.session.add(AISettings(enabled=True, caption_enabled=False))
        db.session.commit()
    login(user)
    r = client.post("/social/api/ai/caption", data={"task_id": task.id})
    assert r.status_code == 503


def test_invoke_retries_once_on_transient(monkeypatch):
    from app.ai import service
    from app.ai.errors import AITransient
    monkeypatch.setattr(service.time, "sleep", lambda s: None)   # no real delay
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise AITransient("busy")
        return "ok"

    assert service._invoke(flaky) == "ok"
    assert calls["n"] == 2                                       # retried once


def test_invoke_gives_up_after_one_retry(monkeypatch):
    from app.ai import service
    from app.ai.errors import AITransient
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always():
        calls["n"] += 1
        raise AITransient("still busy")

    with pytest.raises(AITransient):
        service._invoke(always)
    assert calls["n"] == 2                                       # tried twice, no more


def test_unknown_model_warns_but_still_saves(client, login, make_user, app):
    login(make_user("admin"))
    r = client.post("/admin/ai/", data={
        "enabled": "on",
        "caption_provider": "gemini", "caption_model": "gemini-typo-9",
    }, follow_redirects=True)
    assert b"isn" in r.data and b"known list" in r.data          # warning shown
    with app.app_context():
        row = AISettings.query.first()
        assert row.caption_model == "gemini-typo-9"             # saved anyway


def test_known_model_does_not_warn(client, login, make_user):
    login(make_user("admin"))
    r = client.post("/admin/ai/", data={
        "enabled": "on",
        "caption_provider": "gemini", "caption_model": "gemini-flash-latest",
    }, follow_redirects=True)
    assert b"known list" not in r.data


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


# -- Claude adapter: selectable, import-safe, keyless fails cleanly ---------

def test_claude_is_a_selectable_provider(app):
    assert "claude" in ai_settings.VALID_PROVIDERS
    with app.test_request_context():
        keys = {p["key"] for p in ai_settings.catalog_for_ui()}
        assert "claude" in keys


def test_claude_adapter_import_safe_and_keyless_raises_auth():
    from app.ai.base import CaptionContext, ReplyContext
    from app.ai.errors import AIAuth
    from app.ai.providers.claude import ClaudeProvider

    prov = ClaudeProvider(model="claude-sonnet-5", api_key=None)
    # Keyless must raise AIAuth before any network/SDK import.
    with pytest.raises(AIAuth):
        prov.generate_caption(CaptionContext(brief="hi", platforms=["twitter"]))
    with pytest.raises(AIAuth):
        prov.generate_reply(ReplyContext(review_text="Great!", rating=5))


def test_claude_resolves_own_model_when_provider_overridden(app):
    # Provider switched to Claude, model blank -> Claude's own first model,
    # never the (Gemini) env default.
    with app.app_context():
        db.session.add(AISettings(caption_provider="claude"))
        db.session.commit()
    with app.test_request_context():
        provider, model = ai_settings.resolve("caption")
        assert provider == "claude"
        assert model == "claude-sonnet-5"
        assert model != app.config["AI_CAPTION_MODEL"]
