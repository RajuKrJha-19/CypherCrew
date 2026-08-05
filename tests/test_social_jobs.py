"""Background jobs + the Status/Activity screen: a tracked job records a row and
the Activity route renders. The worker runs in a daemon thread, so we assert on
the synchronous parts (row creation + the route) rather than thread timing.
"""
from app.models import BackgroundJob
from app.social import jobs


def test_jobs_start_creates_a_tracked_row(app):
    with app.test_request_context():
        jid = jobs.start("fetch_comments", lambda: {"message": "ok"})
    with app.app_context():
        row = BackgroundJob.query.get(jid)
        assert row is not None
        assert row.kind == "fetch_comments"
        # 'running' at creation; may already be 'done' if the thread finished.
        assert row.status in ("running", "done")


def test_a_failed_job_records_the_real_error_not_a_vague_pointer(app):
    """A failure must be READABLE on the Status screen, not "see the server
    log". The row's message carries the actual exception, and result carries a
    traceback for tracing."""
    def boom():
        raise ValueError("channel token is missing")

    with app.test_request_context():
        jid = jobs.start("fetch_comments", boom)
    with app.app_context():
        row = BackgroundJob.query.get(jid)
        assert row.status == "failed"
        assert "ValueError" in row.message
        assert "channel token is missing" in row.message
        assert "see the server log" not in row.message.lower()
        assert row.result and "traceback" in row.result


def test_recent_and_running_count(app):
    with app.app_context():
        from app.extensions import db
        db.session.add(BackgroundJob(kind="auto_reply", status="running"))
        db.session.add(BackgroundJob(kind="fetch_comments", status="done"))
        db.session.commit()
    with app.test_request_context():
        assert jobs.running_count() >= 1
        kinds = {j.kind for j in jobs.recent(limit=50)}
        assert "auto_reply" in kinds and "fetch_comments" in kinds


def test_activity_route_renders(client, login, make_user):
    login(make_user("employee", permissions=["manage_social"]))
    r = client.get("/social/activity")
    assert r.status_code == 200
    assert b"Status" in r.data
