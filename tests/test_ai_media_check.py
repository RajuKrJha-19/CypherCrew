"""AI Media QA (Phase 2): on-demand advisory check of a submitted deliverable.

All through the SimulationProvider - no key, no network. Covers the service
(persist + the image/PDF/video/unreadable paths), the route, and the gates.
"""
from app.ai import service as ai_service
from app.extensions import db
from app.models import AICheck, Task


# -- logo / visual-identity reference in the QA prompt ----------------------

def test_media_prompt_adds_logo_check_when_reference_present():
    from app.ai import prompts
    from app.ai.base import MediaCheckContext, MediaInput
    ctx = MediaCheckContext(
        brief="Diwali poster",
        references=[MediaInput(data=b"x", mime_type="image/png",
                               label="official logo")])
    system, user = prompts.media_check_prompt(ctx)
    assert "LOGO" in system and "reference" in system.lower()
    # The model is told which image is the deliverable vs the reference.
    assert "FIRST attached image is the deliverable" in user


def test_media_prompt_has_no_image_order_note_without_reference():
    from app.ai import prompts
    from app.ai.base import MediaCheckContext
    _system, user = prompts.media_check_prompt(MediaCheckContext(brief="x"))
    assert "FIRST attached image is the deliverable" not in user


def test_check_media_unreadable_file_is_clean_info_and_persisted(
        app, make_task_file):
    tf = make_task_file(mime_type="image/png", filename="poster.png")
    with app.test_request_context():
        out = ai_service.check_media(tf, created_by_id=None)

    assert out["status"] == "clean"                # info-only -> clean
    assert out["findings"] and out["findings"][0]["severity"] == "info"
    assert out["model"] == "simulation"
    with app.app_context():
        assert AICheck.query.get(out["check_id"]) is not None


def test_check_media_video_is_unsupported_note(app, make_task_file):
    tf = make_task_file(mime_type="video/mp4", filename="clip.mp4")
    with app.test_request_context():
        out = ai_service.check_media(tf)
    assert out["status"] == "clean"
    assert "images and PDFs" in out["findings"][0]["message"]


def test_check_media_flags_from_provider(app, make_task_file, monkeypatch):
    tf = make_task_file(mime_type="image/png", filename="poster.png")
    # Drive the simulator's warning path via the brief, and make the file
    # "readable" so the provider actually runs.
    with app.app_context():
        task = Task.query.get(tf.task_id)
        task.description = "SIMWARN deliverable off-brief"
        db.session.commit()
    from app.storage.storage_service import StorageService
    monkeypatch.setattr(StorageService, "read_bytes",
                        lambda self, object_key: b"fake-image-bytes")

    with app.test_request_context():
        out = ai_service.check_media(tf)

    assert out["status"] == "flagged"
    assert any(f["severity"] == "warning" for f in out["findings"])


# -- Route + gates ----------------------------------------------------------

def _reviewer(make_user):
    # view_all_tasks -> can_view_task; approve_tasks -> can_review.
    return make_user("employee",
                     permissions=["view_all_tasks", "approve_tasks"])


def test_ai_check_route_runs_for_a_reviewer(
        client, login, make_user, make_task_file):
    tf = make_task_file(mime_type="image/png", filename="poster.png")
    login(_reviewer(make_user))
    r = client.post(f"/tasks/files/{tf.id}/ai-check")
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] in ("clean", "flagged")
    assert "findings" in data


def test_ai_check_route_forbidden_for_an_outsider(
        client, login, make_user, make_task_file):
    tf = make_task_file(mime_type="image/png", filename="poster.png")
    login(make_user("employee"))          # not assignee, not a reviewer
    r = client.post(f"/tasks/files/{tf.id}/ai-check")
    assert r.status_code == 403


def test_ai_check_route_unavailable_when_ai_off(
        client, login, make_user, make_task_file):
    tf = make_task_file(mime_type="image/png", filename="poster.png")
    login(_reviewer(make_user))
    client.application.config["AI_ENABLED"] = False
    try:
        r = client.post(f"/tasks/files/{tf.id}/ai-check")
        assert r.status_code == 503
    finally:
        client.application.config["AI_ENABLED"] = True


def test_ai_check_route_404_for_missing_file(client, login, make_user):
    login(_reviewer(make_user))
    r = client.post("/tasks/files/99999999/ai-check")
    assert r.status_code == 404
