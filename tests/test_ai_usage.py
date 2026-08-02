"""AI cost/usage tracking + the monthly budget cap.

All through the SimulationProvider (0-cost calls), so the log, the summary, the
budget gate and the admin page are exercised with no key and no network.
"""
import pytest

from app.ai import pricing, service as ai_service, usage as ai_usage
from app.extensions import db
from app.models import AISettings, AIUsage


@pytest.fixture(autouse=True)
def _clean_usage(app):
    """ai_usage + ai_settings are global (not test-prefixed); wipe around each
    test so totals and the budget cap are deterministic."""
    def wipe():
        with app.app_context():
            AIUsage.query.delete()
            AISettings.query.delete()
            db.session.commit()
    wipe()
    yield
    wipe()


# -- pricing (pure) ---------------------------------------------------------

def test_pricing_known_costs_and_unknown_is_zero():
    assert pricing.estimate("gemini-2.5-flash", 1_000_000, 1_000_000) > 0
    assert pricing.estimate("simulation", 1000, 5000) == 0.0
    assert pricing.estimate("some-unlisted-model", 1_000_000, 1_000_000) == 0.0


# -- record + monthly total -------------------------------------------------

def test_record_writes_a_row_and_total_reflects_it(app):
    with app.test_request_context():
        ai_usage.record(feature="caption", provider="gemini",
                        model="gemini-2.5-pro",
                        input_tokens=1_000_000, output_tokens=1_000_000)
    with app.app_context():
        assert AIUsage.query.count() == 1
    with app.test_request_context():
        assert ai_usage.month_total_usd() > 0


# -- budget cap -------------------------------------------------------------

def test_within_budget_no_cap_then_over(app):
    with app.test_request_context():
        assert ai_usage.within_budget() is True          # no row -> no cap
    with app.app_context():
        db.session.add(AISettings(monthly_budget_usd=0.001))
        db.session.add(AIUsage(feature="caption", provider="gemini",
                               model="x", est_cost_usd=1.0))
        db.session.commit()
    with app.test_request_context():
        assert ai_usage.within_budget() is False


# -- the service logs usage on every call -----------------------------------

def test_generate_caption_logs_a_usage_row(app):
    with app.test_request_context():
        ai_service.generate_caption(brief="hi", platforms=["twitter"])
    with app.app_context():
        row = AIUsage.query.filter_by(feature="caption").first()
        assert row is not None
        assert row.provider == "simulation"
        assert row.est_cost_usd == 0.0        # simulation is free


# -- the cap gates the live route -------------------------------------------

def test_budget_cap_blocks_the_caption_route(
        client, login, make_user, make_task, app):
    user = make_user("employee", permissions=["manage_social"])
    task = make_task(user)
    with app.app_context():
        db.session.add(AISettings(monthly_budget_usd=0.001))
        db.session.add(AIUsage(feature="caption", provider="gemini",
                               model="x", est_cost_usd=5.0))
        db.session.commit()
    login(user)
    r = client.post("/social/api/ai/caption", data={"task_id": task.id})
    assert r.status_code == 503


# -- admin usage page -------------------------------------------------------

def test_usage_page_renders_for_admin(client, login, make_user):
    login(make_user("admin"))
    r = client.get("/admin/ai/usage")
    assert r.status_code == 200
    assert b"AI Usage" in r.data


def test_usage_page_forbidden_for_non_admin(client, login, make_user):
    login(make_user("employee"))
    assert client.get("/admin/ai/usage").status_code == 403
