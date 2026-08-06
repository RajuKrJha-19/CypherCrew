"""Run a user-triggered action as a tracked background job.

The point is VISIBILITY: an action like "Fetch comments" or "Run auto-reply"
can take tens of seconds (an AI call + a Graph call per item), so running it in
the request thread hangs the browser and drops the connection. Here it runs in
a daemon thread and writes its outcome to a BackgroundJob row, which the
Activity screen shows - the person can see it running and what it did instead
of staring at a spinner.

Deliberately no external queue (no Celery/Redis): a thread + one DB row is
enough for this workload, and it degrades gracefully - if the worker dies the
row simply stays "running" and the user can retrigger.
"""
import threading
import traceback
from datetime import datetime, timedelta

from flask import current_app

from app.extensions import db
from app.models import BackgroundJob


#: A row that has sat at "running" for longer than this is treated as dead
#: rather than in-flight. The worker is an unsupervised daemon thread, so a
#: restart mid-job leaves its row "running" forever (see the module docstring);
#: without a cutoff a single crash would block that action permanently.
STALE_AFTER = timedelta(minutes=15)


def is_running(kind, client_id=None):
    """Is a job of this kind already in flight for this scope?

    Used to refuse a duplicate trigger. Two concurrent comment syncs walk the
    same rows, and the second one queues behind the first's locks until
    Postgres cancels its statement - which surfaced as a "canceling statement
    due to statement timeout" on an ordinary Fetch.
    """
    q = BackgroundJob.query.filter(
        BackgroundJob.kind == kind,
        BackgroundJob.status == "running",
        BackgroundJob.started_at >= datetime.utcnow() - STALE_AFTER)
    if client_id is not None:
        # A global run (client_id NULL) covers every client, so it conflicts
        # with a per-client trigger as well.
        q = q.filter(db.or_(BackgroundJob.client_id == client_id,
                            BackgroundJob.client_id.is_(None)))
    return db.session.query(q.exists()).scalar()


def start(kind, fn, *, client_id=None, actor_id=None):
    """Create a 'running' BackgroundJob, run fn() in a background thread, and
    write its outcome back to the row. Returns the job id immediately.

    fn() runs inside a fresh app context (its own thread-local DB session) and
    should return a dict; its optional 'message' key becomes the row's one-line
    summary and the rest is stored as `result`. Any exception -> 'failed',
    logged. Never raises to the caller."""
    app = current_app._get_current_object()
    job = BackgroundJob(kind=kind, client_id=client_id, status="running",
                        started_by_id=actor_id, started_at=datetime.utcnow())
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    def _run():
        with app.app_context():
            _finish(job_id, kind, fn)

    _spawn(_run, kind, job_id)
    return job_id


def _finish(job_id, kind, fn):
    row = BackgroundJob.query.get(job_id)
    if row is None:
        return
    try:
        out = fn()
        out = out if isinstance(out, dict) else {}
        row.message = out.pop("message", None) or "Done."
        row.result = out or None
        row.status = "done"
    except Exception as exc:  # noqa: BLE001 - surfaced on the row + logged
        # The whole point of this screen is that a failure is READABLE here,
        # not buried in a log file the user can't open. So the real error goes
        # ON the row: a one-line "<Type>: <message>" as the summary, and the
        # traceback tail in `result` for tracing — never a vague "see the log".
        current_app.logger.exception("[jobs] %s (job %s) failed", kind, job_id)
        db.session.rollback()
        summary = f"{type(exc).__name__}: {exc}".strip()
        tb = traceback.format_exc()
        row = BackgroundJob.query.get(job_id)
        if row is not None:
            row.status = "failed"
            row.message = summary[:300] or "Failed (no error text)."
            # Last frames of the traceback — enough to locate it, bounded so a
            # giant trace never bloats the row.
            row.result = {"error": summary[:1000],
                          "traceback": tb[-1800:]}
    if row is not None:
        row.finished_at = datetime.utcnow()
        db.session.commit()


def _spawn(run, kind, job_id):
    """Start the job runner in a daemon thread. Isolated here so the test suite
    can stub it to run synchronously — a real thread committing on a separate
    connection races the shared test DB (see conftest._sync_jobs)."""
    threading.Thread(target=run, name=f"job-{kind}-{job_id}", daemon=True).start()


def recent(limit=40, client_id=None):
    """Newest background jobs first, for the Activity screen."""
    q = BackgroundJob.query
    if client_id:
        q = q.filter(BackgroundJob.client_id == client_id)
    return q.order_by(BackgroundJob.id.desc()).limit(limit).all()


def running_count():
    return BackgroundJob.query.filter_by(status="running").count()
