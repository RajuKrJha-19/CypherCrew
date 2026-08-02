"""Engage AI comment-reply assistant: the comment-aware prompt, the simulated
draft, the service helper, and the /engage/<id>/ai-draft route + gates.
Simulation stays on, so nothing calls a real provider. Draft-only - the route
never posts anything back to a platform.
"""
from datetime import datetime

from app.extensions import db
from app.models import SocialComment


def _comment(session, target, message="Do you deliver to Pune?",
             author="Priya Nair"):
    c = SocialComment(
        target_id=target.id, platform=target.platform,
        external_id=f"cmt-{target.id}-{message[:8]}",
        author_name=author, message=message,
        is_ours=False, status="open", fetched_at=datetime.utcnow())
    session.add(c)
    session.commit()
    return c


# -- prompt + simulated draft -----------------------------------------------

def test_comment_reply_prompt_is_comment_shaped():
    from app.ai import prompts
    from app.ai.base import ReplyContext
    system, user = prompts.reply_prompt(ReplyContext(
        kind="comment", review_text="Do you deliver to Pune?",
        reviewer="Priya", facts="Delivery: Pune, Mumbai",
        post_context="Diwali offer week"))
    # Comment-shaped, not review-shaped.
    assert "comment" in system.lower()
    assert "review" not in system.lower()
    # Carries the commenter, the question, the post context and the facts.
    assert "Priya" in user and "Pune" in user
    assert "Diwali offer week" in user
    assert "Delivery: Pune, Mumbai" in user


def test_sim_comment_reply_is_helpful():
    from app.ai.providers.simulation import SimulationProvider
    from app.ai.base import ReplyContext
    out = SimulationProvider().generate_reply(
        ReplyContext(kind="comment", reviewer="Sam Jones"))
    assert "Sam" in out
    assert "simulated" in out.lower()


def test_service_generate_comment_reply_returns_text(app):
    from app.ai import service as ai_service
    with app.test_request_context():
        reply = ai_service.generate_comment_reply(
            comment_text="Love this!", author="Aditi",
            facts="Phone: 91234")
    assert reply and "Aditi" in reply


# -- route + gates ----------------------------------------------------------

def test_ai_draft_route_returns_reply(session, client, login, make_user,
                                      make_target):
    _, _, target = make_target()
    comment = _comment(session, target)
    login(make_user("employee", permissions=["manage_social"]))
    r = client.post(f"/social/engage/{comment.id}/ai-draft")
    assert r.status_code == 200
    assert r.get_json()["reply"]


def test_ai_draft_forbidden_without_social(session, client, login, make_user,
                                           make_target):
    _, _, target = make_target()
    comment = _comment(session, target)
    login(make_user("employee"))                 # no manage_social
    assert client.post(f"/social/engage/{comment.id}/ai-draft").status_code == 403


def test_ai_draft_rejects_empty_comment(session, client, login, make_user,
                                        make_target):
    _, _, target = make_target()
    comment = _comment(session, target, message="")
    login(make_user("employee", permissions=["manage_social"]))
    assert client.post(f"/social/engage/{comment.id}/ai-draft").status_code == 400


def test_ai_draft_blocked_when_ai_soft_disabled(session, client, login,
                                                make_user, make_target, app):
    from app.models import AISettings
    _, _, target = make_target()
    comment = _comment(session, target)
    with app.app_context():
        AISettings.query.delete()
        db.session.add(AISettings(enabled=False))
        db.session.commit()
    login(make_user("employee", permissions=["manage_social"]))
    try:
        r = client.post(f"/social/engage/{comment.id}/ai-draft")
        assert r.status_code == 503
    finally:
        with app.app_context():
            AISettings.query.delete()
            db.session.commit()


def test_ai_draft_blocked_when_comment_feature_off(session, client, login,
                                                   make_user, make_target, app):
    # Master ON, but the Comment-replies feature turned off individually.
    from app.models import AISettings
    _, _, target = make_target()
    comment = _comment(session, target)
    with app.app_context():
        AISettings.query.delete()
        db.session.add(AISettings(enabled=True, comment_enabled=False))
        db.session.commit()
    login(make_user("employee", permissions=["manage_social"]))
    try:
        assert client.post(
            f"/social/engage/{comment.id}/ai-draft").status_code == 503
    finally:
        with app.app_context():
            AISettings.query.delete()
            db.session.commit()
