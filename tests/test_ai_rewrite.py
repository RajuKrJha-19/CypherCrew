"""AI caption Rewrite toolbar (Shorten/Expand/Rephrase/formal/casual/emojis/
grammar), exercised through the SimulationProvider - no key, no network. Covers
the provider, the prompt, the service, and the composer route incl. its gates.
"""
import pytest

from app.ai.base import RewriteContext
from app.ai.errors import AIPermanent
from app.ai.prompts import _REWRITE_ACTIONS, rewrite_prompt
from app.ai.providers.gemini import GeminiProvider
from app.ai.providers.simulation import SimulationProvider


# --------------------------------------------------------------------------
# Provider - the deterministic backend tests and localhost run on
# --------------------------------------------------------------------------

@pytest.mark.parametrize("action", sorted(_REWRITE_ACTIONS))
def test_sim_rewrite_returns_nonempty_text_for_every_action(action):
    prov = SimulationProvider()
    out = prov.rewrite_caption(RewriteContext(
        text="Grand opening this weekend — come celebrate with us!",
        action=action))
    assert out and out.strip()


def test_sim_rewrite_reflects_the_action():
    prov = SimulationProvider()
    base = "Visit our new lakeside villas today."
    assert "✨" in prov.rewrite_caption(RewriteContext(text=base, action="emojis"))
    longer = prov.rewrite_caption(RewriteContext(text=base, action="expand"))
    assert len(longer) > len(base)


def test_provider_rewrite_raises_on_empty_output_no_silent_fallback(monkeypatch):
    # A real provider that comes back empty must RAISE (visible error), never
    # quietly hand back the original caption as if the rewrite worked.
    prov = GeminiProvider(model="x", api_key="k")
    monkeypatch.setattr(prov, "_generate", lambda *a, **k: "   ")
    with pytest.raises(AIPermanent):
        prov.rewrite_caption(RewriteContext(text="hello", action="shorten"))


# --------------------------------------------------------------------------
# Prompt - action instruction + facts/limits flow in; text-only out
# --------------------------------------------------------------------------

def test_rewrite_prompt_carries_action_facts_and_asks_text_only():
    system, user = rewrite_prompt(RewriteContext(
        text="Old caption", action="shorten",
        facts="Official phone: 91234", tone="premium",
        platforms=["twitter"], caption_limits={"twitter": 280}))
    assert "shorter" in system.lower()          # the shorten instruction
    assert "premium" in system.lower()          # tone honored
    assert "only" in system.lower()             # "Return ONLY the ... text"
    assert "91234" in user                      # Client Brain facts injected
    assert "Old caption" in user                # the text to rewrite


def test_rewrite_actions_are_the_expected_set():
    assert set(_REWRITE_ACTIONS) == {
        "shorten", "expand", "rephrase", "formal",
        "casual", "emojis", "grammar", "keywords"}


def test_keywords_action_appends_a_line_not_replaces(app):
    from app.ai import service as ai_service
    with app.test_request_context():
        out = ai_service.rewrite_caption(
            text="Admissions open at Sandip University.", action="keywords")
    # The original caption survives and a keyword line is added below it.
    assert out["caption"].startswith("Admissions open at Sandip University.")
    assert "\n" in out["caption"]


# --------------------------------------------------------------------------
# Service - returns {caption, ai_usage_id}
# --------------------------------------------------------------------------

def test_service_rewrite_returns_caption_and_usage_id(app):
    from app.ai import service as ai_service
    with app.test_request_context():
        out = ai_service.rewrite_caption(
            text="Come see our brand new villas!", action="shorten")
    assert out["caption"]
    assert out["ai_usage_id"] is not None       # a caption-tier spend row logged


# --------------------------------------------------------------------------
# Route - the composer endpoint + its gates
# --------------------------------------------------------------------------

def test_rewrite_route_transforms_the_caption(client, login, make_user):
    user = make_user("employee", permissions=["manage_social"])
    login(user)
    r = client.post("/social/api/ai/caption/rewrite",
                    data={"text": "Visit our villas today.", "action": "emojis"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["caption"] and "✨" in data["caption"]
    assert "ai_usage_id" in data


def test_rewrite_route_rejects_unknown_action(client, login, make_user):
    user = make_user("employee", permissions=["manage_social"])
    login(user)
    r = client.post("/social/api/ai/caption/rewrite",
                    data={"text": "hi", "action": "translate"})
    assert r.status_code == 400


def test_rewrite_route_needs_text(client, login, make_user):
    user = make_user("employee", permissions=["manage_social"])
    login(user)
    r = client.post("/social/api/ai/caption/rewrite",
                    data={"action": "shorten"})
    assert r.status_code == 400


def test_rewrite_route_rejects_overlong_text(client, login, make_user):
    user = make_user("employee", permissions=["manage_social"])
    login(user)
    r = client.post("/social/api/ai/caption/rewrite",
                    data={"text": "x" * 8001, "action": "shorten"})
    assert r.status_code == 400


def test_rewrite_route_unavailable_when_ai_off(client, login, make_user):
    user = make_user("employee", permissions=["manage_social"])
    login(user)
    client.application.config["AI_ENABLED"] = False
    try:
        r = client.post("/social/api/ai/caption/rewrite",
                        data={"text": "hi", "action": "shorten"})
        assert r.status_code == 503
    finally:
        client.application.config["AI_ENABLED"] = True


def test_rewrite_route_blocked_over_budget(client, login, make_user, monkeypatch):
    user = make_user("employee", permissions=["manage_social"])
    login(user)
    monkeypatch.setattr("app.ai.usage.within_budget", lambda: False)
    r = client.post("/social/api/ai/caption/rewrite",
                    data={"text": "hi", "action": "shorten"})
    assert r.status_code == 503


def test_rewrite_route_forbidden_without_permission(client, login, make_user):
    user = make_user("employee")           # no manage_social
    login(user)
    r = client.post("/social/api/ai/caption/rewrite",
                    data={"text": "hi", "action": "shorten"})
    assert r.status_code == 403


def test_rewrite_route_forbidden_for_an_unviewable_task(
        client, login, make_user, make_task):
    owner = make_user("video_editor")
    task = make_task(owner)
    outsider = make_user("employee", permissions=["manage_social"])
    login(outsider)
    r = client.post("/social/api/ai/caption/rewrite",
                    data={"text": "hi", "action": "shorten", "task_id": task.id})
    assert r.status_code == 403
