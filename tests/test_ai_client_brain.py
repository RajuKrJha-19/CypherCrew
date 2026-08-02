"""Client Brain (structured knowledgebase) + the fact-checker that reads it.

Storage round-trip, the facts-text builder, and check_media flagging a fact
error from the brain - all through the SimulationProvider (no key, no network).
"""
from app.ai import client_brain
from app.ai import service as ai_service
from app.extensions import db
from app.models import Client, Task
from tests.conftest import PYTEST_EMAIL_PREFIX


# -- client_brain module (pure) ---------------------------------------------

def test_from_form_builds_and_drops_empty_sections():
    form = {"bb_official_phones": "  91234  ", "bb_official_emails": "",
            "bb_dos": "Be bold", "ignored": "x"}
    assert client_brain.from_form(form) == {
        "official_phones": "91234", "dos": "Be bold"}


def test_from_form_is_none_when_nothing_filled():
    assert client_brain.from_form({}) is None


def test_facts_text_excludes_internal_notes():
    class _C:
        brand_brain = {"official_phones": "91234",
                       "internal_notes": "team-secret"}
    text = client_brain.facts_text(_C())
    assert "91234" in text
    assert "team-secret" not in text     # internal notes are never AI-fed


def test_facts_text_empty_without_brain():
    class _C:
        brand_brain = None
    assert client_brain.facts_text(_C()) == ""


# -- fact-checker uses the brain --------------------------------------------

def test_check_media_flags_a_fact_error_from_the_brain(
        app, make_task_file, monkeypatch):
    tf = make_task_file(mime_type="image/png", filename="poster.png")
    with app.app_context():
        task = Task.query.get(tf.task_id)
        # Brain fact carries the simulator's fact sentinel.
        task.client.brand_brain = {"official_phones": "SIMFACT 91234"}
        db.session.commit()
    from app.storage.storage_service import StorageService
    monkeypatch.setattr(StorageService, "read_bytes",
                        lambda self, object_key: b"fake-image")

    with app.test_request_context():
        out = ai_service.check_media(tf)

    assert out["status"] == "flagged"
    assert any(f["category"] == "fact" and f["severity"] == "error"
               for f in out["findings"])


# -- edit-client saves the brain --------------------------------------------

def test_edit_client_saves_brand_brain(client, login, make_user, app):
    admin = make_user("admin")                        # management -> can manage
    with app.app_context():
        c = Client(client_name=f"{PYTEST_EMAIL_PREFIX}brain", status="active")
        db.session.add(c)
        db.session.commit()
        cid = c.id

    login(admin)
    r = client.post(f"/clients/{cid}/edit", data={
        "client_name": f"{PYTEST_EMAIL_PREFIX}brain", "status": "active",
        "bb_official_phones": "98765", "bb_dos": "Be bold",
        "bb_internal_notes": "",
    })
    assert r.status_code in (302, 303)
    with app.app_context():
        saved = Client.query.get(cid)
        assert saved.brand_brain == {"official_phones": "98765", "dos": "Be bold"}
