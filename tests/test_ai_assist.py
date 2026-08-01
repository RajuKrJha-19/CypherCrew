"""AI Assist (Phase 1: captions + alt-text), exercised entirely through the
SimulationProvider - no provider key, no network. Covers the provider, the
service, and the composer routes incl. the AI_ENABLED + permission gates.
"""
import pytest

from app.ai.base import CaptionContext, MediaCheckContext, MediaInput
from app.ai.errors import AIDisabled
from app.ai.providers.simulation import SimulationProvider
from app.ai.registry import get_provider


# --------------------------------------------------------------------------
# SimulationProvider - the deterministic backend tests and localhost run on
# --------------------------------------------------------------------------

def test_sim_caption_respects_limits_and_carries_brand_voice():
    prov = SimulationProvider()
    ctx = CaptionContext(
        brief="Kargil Vijay Diwas tribute post",
        industry="Real Estate",
        brand_voice="warm and premium",
        platforms=["twitter", "instagram"],
        caption_limits={"twitter": 280, "instagram": 2200},
    )
    res = prov.generate_caption(ctx)

    assert res.caption
    assert "warm and premium" in res.caption          # brand field flows through
    assert set(res.per_platform) == {"twitter", "instagram"}
    assert len(res.per_platform["twitter"]) <= 280     # per-platform clamp
    assert isinstance(res.hashtags, list)


def test_sim_alt_text_is_a_short_sentence():
    prov = SimulationProvider()
    alt = prov.generate_alt_text(
        MediaInput(data=b"x", mime_type="image/png", label="poster"))
    assert alt and len(alt) <= 125


def test_sim_media_check_clean_and_flagged():
    prov = SimulationProvider()
    clean = prov.check_media(MediaCheckContext(brief="on-brief deliverable"))
    assert clean and clean[0].severity == "info"

    flagged = prov.check_media(MediaCheckContext(brief="SIMWARN off brief"))
    assert any(f.severity == "warning" for f in flagged)


# --------------------------------------------------------------------------
# Registry - resolves the backend, fail-closed when AI is off
# --------------------------------------------------------------------------

def test_registry_resolves_simulation(app):
    with app.test_request_context():
        assert get_provider().key == "simulation"


def test_registry_is_disabled_when_flag_off(app):
    with app.test_request_context():
        app.config["AI_ENABLED"] = False
        try:
            with pytest.raises(AIDisabled):
                get_provider()
        finally:
            app.config["AI_ENABLED"] = True


# --------------------------------------------------------------------------
# Service - gathers context (caption limits from the social registry) and
# returns plain dicts. Missing/unreadable media is skipped, never fatal.
# --------------------------------------------------------------------------

def test_service_generate_caption_pulls_platform_limits(app):
    with app.test_request_context():
        out = ai_generate_caption(app)
    assert out["caption"]
    assert "twitter" in out["per_platform"]
    # 280 is Twitter's real Capabilities.max_caption_chars, sourced live.
    assert len(out["per_platform"]["twitter"]) <= 280


def ai_generate_caption(app):
    from app.ai import service as ai_service
    return ai_service.generate_caption(
        brief="Launch our premium lakeside villas this weekend.",
        platforms=["twitter"],
        media=[("does/not/exist.png", None)],   # unreadable -> skipped
    )


def test_service_alt_text_empty_when_unreadable(app):
    from app.ai import service as ai_service
    with app.test_request_context():
        alt = ai_service.generate_alt_text("does/not/exist.png")
    assert alt == ""     # nothing to describe, but no error


# --------------------------------------------------------------------------
# Routes - the composer endpoints, incl. the gates
# --------------------------------------------------------------------------

def test_caption_route_drafts_from_a_task(client, login, make_user, make_task):
    user = make_user("employee", permissions=["manage_social"])
    task = make_task(user)          # default prefixed title (kept purgeable)
    login(user)

    r = client.post("/social/api/ai/caption",
                    data={"task_id": task.id, "platforms": "twitter"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["caption"]
    assert "twitter" in data["per_platform"]


def test_caption_route_needs_a_brief_or_media(client, login, make_user):
    user = make_user("employee", permissions=["manage_social"])
    login(user)
    r = client.post("/social/api/ai/caption", data={"platforms": "twitter"})
    assert r.status_code == 400


def test_caption_route_forbidden_without_permission(
        client, login, make_user, make_task):
    user = make_user("employee")           # no manage_social
    task = make_task(user)
    login(user)
    r = client.post("/social/api/ai/caption", data={"task_id": task.id})
    assert r.status_code == 403


def test_caption_route_unavailable_when_ai_off(
        client, login, make_user, make_task):
    user = make_user("employee", permissions=["manage_social"])
    task = make_task(user)
    login(user)
    client.application.config["AI_ENABLED"] = False
    try:
        r = client.post("/social/api/ai/caption", data={"task_id": task.id})
        assert r.status_code == 503
    finally:
        client.application.config["AI_ENABLED"] = True


def test_alt_text_route_requires_a_key(client, login, make_user):
    user = make_user("employee", permissions=["manage_social"])
    login(user)
    r = client.post("/social/api/ai/alt-text", data={})
    assert r.status_code == 400


# -- Object-level authorization (IDOR guard) --------------------------------

def test_caption_route_forbidden_for_an_unviewable_task(
        client, login, make_user, make_task):
    owner = make_user("video_editor")                    # the assignee
    task = make_task(owner)
    outsider = make_user("employee", permissions=["manage_social"])
    login(outsider)                                      # can_use_social, not a viewer
    r = client.post("/social/api/ai/caption", data={"task_id": task.id})
    assert r.status_code == 403


def test_alt_text_allows_an_ephemeral_upload_key(client, login, make_user):
    user = make_user("employee", permissions=["manage_social"])
    login(user)
    r = client.post("/social/api/ai/alt-text",
                    data={"object_key": "social_uploads/abc123_poster.png"})
    assert r.status_code == 200          # allowed; unreadable in tests -> ""
    assert r.get_json()["alt_text"] == ""


def test_alt_text_forbidden_for_an_unviewable_task_file(
        client, login, make_user, make_task_file):
    tf = make_task_file(mime_type="image/png", filename="poster.png")
    outsider = make_user("employee", permissions=["manage_social"])
    login(outsider)
    r = client.post("/social/api/ai/alt-text",
                    data={"object_key": tf.object_key})
    assert r.status_code == 403
