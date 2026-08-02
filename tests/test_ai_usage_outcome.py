"""AI keep-rate ROI signal: AIUsage.outcome, usage.set_outcome, the caption
service returning its usage id, the outcome-report route, and the month
summary's keep-rate. Shared dev DB, so every test cleans only the rows it made.
"""
import pytest

from app.extensions import db
from app.models import AIUsage

_created = []


def _mk(app, feature="caption", actor_id=None):
    from app.ai import usage
    with app.app_context():
        uid = usage.record(feature=feature, provider="simulation",
                           model="simulation", actor_id=actor_id)
    _created.append(uid)
    return uid


@pytest.fixture(autouse=True)
def _cleanup(app):
    yield
    ids = [i for i in _created if i]
    if ids:
        with app.app_context():
            AIUsage.query.filter(AIUsage.id.in_(ids)).delete(
                synchronize_session=False)
            db.session.commit()
    _created.clear()


# -- record + set_outcome ---------------------------------------------------

def test_record_returns_id_and_outcome_is_write_once(app):
    from app.ai import usage
    uid = _mk(app)
    with app.app_context():
        assert uid is not None
        assert usage.set_outcome(uid, "used") is True
        assert AIUsage.query.get(uid).outcome == "used"
        # Never overwrites an existing outcome.
        assert usage.set_outcome(uid, "discarded") is False
        assert AIUsage.query.get(uid).outcome == "used"


def test_set_outcome_rejects_unknown_value(app):
    from app.ai import usage
    uid = _mk(app)
    with app.app_context():
        assert usage.set_outcome(uid, "bogus") is False
        assert AIUsage.query.get(uid).outcome is None


def test_set_outcome_is_owner_scoped(app, make_user):
    from app.ai import usage
    owner = make_user("employee", permissions=["manage_social"])
    uid = _mk(app, actor_id=owner.id)               # real FK; actor below is only compared
    with app.app_context():
        assert usage.set_outcome(uid, "used", actor_id=owner.id + 9999) is False
        assert AIUsage.query.get(uid).outcome is None
        assert usage.set_outcome(uid, "used", actor_id=owner.id) is True


# -- caption service exposes its usage id -----------------------------------

def test_generate_caption_returns_usage_id(app):
    from app.ai import service
    with app.test_request_context():
        out = service.generate_caption(brief="hello world", platforms=["twitter"])
    uid = out.get("ai_usage_id")
    _created.append(uid)
    assert uid is not None


# -- outcome report route ---------------------------------------------------

def test_outcome_route_records_used(app, client, login, make_user):
    user = make_user("employee", permissions=["manage_social"])
    uid = _mk(app, actor_id=user.id)
    login(user)
    r = client.post(f"/social/api/ai/usage/{uid}/outcome", data={"outcome": "used"})
    assert r.status_code == 200
    with app.app_context():
        assert AIUsage.query.get(uid).outcome == "used"


def test_outcome_route_rejects_bad_value(app, client, login, make_user):
    user = make_user("employee", permissions=["manage_social"])
    uid = _mk(app, actor_id=user.id)
    login(user)
    r = client.post(f"/social/api/ai/usage/{uid}/outcome", data={"outcome": "nope"})
    assert r.status_code == 400


def test_outcome_route_forbidden_without_social(app, client, login, make_user):
    uid = _mk(app)
    login(make_user("employee"))                    # no manage_social
    r = client.post(f"/social/api/ai/usage/{uid}/outcome", data={"outcome": "used"})
    assert r.status_code == 403


# -- month summary keep-rate ------------------------------------------------

def test_month_summary_reports_keep_rate(app):
    from app.ai import usage
    u1 = _mk(app, feature="keeprate_probe")
    u2 = _mk(app, feature="keeprate_probe")
    with app.app_context():
        usage.set_outcome(u1, "used")
        usage.set_outcome(u2, "discarded")
        summary = usage.month_summary()
    assert summary["keep"]["keeprate_probe"] == {"used": 1, "discarded": 1}
