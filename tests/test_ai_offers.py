"""Structured, time-limited Client Brain offers: parsing, auto-expiry (the AI
never sees an ended offer), the edit-screen expired flag, and the save.
"""
from datetime import date

from app.ai import client_brain
from app.extensions import db
from app.models import Client
from tests.conftest import PYTEST_EMAIL_PREFIX

TODAY = date(2026, 8, 3)


class _C:
    def __init__(self, offers=None, brain=None):
        self.brand_offers = offers
        self.brand_brain = brain


def test_offers_from_form_builds_and_drops_empty():
    form = {"offer_text_0": "  Diwali 20% off  ", "offer_until_0": "2026-11-10",
            "offer_text_1": "", "offer_until_1": "2026-01-01",       # empty -> dropped
            "offer_text_2": "Always-on freebie", "offer_until_2": ""}
    assert client_brain.offers_from_form(form) == [
        {"text": "Diwali 20% off", "until": "2026-11-10"},
        {"text": "Always-on freebie", "until": None}]


def test_offers_from_form_none_when_empty():
    assert client_brain.offers_from_form({}) is None


def test_valid_offers_excludes_expired():
    c = _C(offers=[{"text": "ended", "until": "2026-07-01"},        # past -> hidden
                   {"text": "live", "until": "2026-12-31"},
                   {"text": "evergreen", "until": None}])
    assert [o["text"] for o in client_brain.valid_offers(c, today=TODAY)] == [
        "live", "evergreen"]


def test_offer_valid_through_its_end_date_inclusive():
    c = _C(offers=[{"text": "ends today", "until": "2026-08-03"}])
    assert client_brain.valid_offers(c, today=TODAY)      # still valid on the day


def test_offers_display_flags_expired():
    c = _C(offers=[{"text": "ended", "until": "2026-07-01"},
                   {"text": "live", "until": "2026-12-31"}])
    rows = client_brain.offers_display(c, today=TODAY)
    assert rows[0]["expired"] is True and rows[1]["expired"] is False


def test_facts_text_includes_only_valid_offers():
    c = _C(brain={"official_phones": "91234"},
           offers=[{"text": "ended offer", "until": "2026-07-01"},
                   {"text": "Diwali 20% off", "until": "2026-11-10"}])
    txt = client_brain.facts_text(c, today=TODAY)
    assert "Diwali 20% off" in txt and "valid until 2026-11-10" in txt
    assert "ended offer" not in txt                       # expired never reaches the AI
    assert "valid TODAY" in txt                           # instruction present


def test_edit_client_saves_structured_offers(client, login, make_user, app):
    login(make_user("admin"))
    with app.app_context():
        c = Client(client_name=f"{PYTEST_EMAIL_PREFIX}offers", status="active")
        db.session.add(c)
        db.session.commit()
        cid = c.id
    client.post(f"/clients/{cid}/edit", data={
        "client_name": f"{PYTEST_EMAIL_PREFIX}offers", "status": "active",
        "offer_text_0": "Summer sale", "offer_until_0": "2026-09-01"})
    with app.app_context():
        assert Client.query.get(cid).brand_offers == [
            {"text": "Summer sale", "until": "2026-09-01"}]
