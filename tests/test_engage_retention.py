"""Comment-PII data-retention purge: a third-party commenter's PII is
anonymised in place once past the retention window, while our own posted
replies and still-recent comments are left untouched, and 0 days disables it.
"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models import SocialComment
from app.social.services import engage


def _mk(session, target, msg, *, is_ours=False, age_days=0, tag=""):
    c = SocialComment(target_id=target.id, platform=target.platform,
                      external_id=f"ret-{msg}-{age_days}-{is_ours}-{tag}",
                      author_name="Sam Jones", author_id="U1",
                      author_pic="https://cdn/pic.jpg", message=msg,
                      is_ours=is_ours, status="open")
    session.add(c)
    session.flush()
    c.created_at = datetime.utcnow() - timedelta(days=age_days)
    session.commit()
    return c


def test_retention_anonymises_old_third_party_pii_only(session, app, make_target):
    _acct, _post, target = make_target()
    old = _mk(session, target, "old public comment", age_days=400)
    recent = _mk(session, target, "recent comment", age_days=1)
    ours = _mk(session, target, "our own reply", is_ours=True, age_days=400)

    with app.test_request_context():
        app.config["ENGAGE_COMMENT_RETENTION_DAYS"] = 180
        out = engage.purge_expired_comment_pii()

    assert out["purged"] == 1
    old = SocialComment.query.get(old.id)
    assert old.author_id is None and old.author_name is None
    assert old.author_pic is None and old.message is None
    # Recent third-party comment: untouched.
    assert SocialComment.query.get(recent.id).author_id == "U1"
    # Our own reply: business record, never treated as third-party PII.
    assert SocialComment.query.get(ours.id).message == "our own reply"


def test_retention_is_idempotent_and_disabled_at_zero(session, app, make_target):
    _acct, _post, target = make_target()
    _mk(session, target, "old", age_days=400)

    with app.test_request_context():
        app.config["ENGAGE_COMMENT_RETENTION_DAYS"] = 0
        disabled = engage.purge_expired_comment_pii()
    assert disabled.get("skipped") == "disabled"

    with app.test_request_context():
        app.config["ENGAGE_COMMENT_RETENTION_DAYS"] = 180
        first = engage.purge_expired_comment_pii()
        second = engage.purge_expired_comment_pii()
    assert first["purged"] == 1
    assert second["purged"] == 0          # already anonymised -> skipped
