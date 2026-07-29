bind = "0.0.0.0:8000"
workers = 2

# Threaded workers, not sync.
#
# With the default sync worker each process serves exactly one request at a
# time, so two workers means two concurrent requests for the whole app. That
# was survivable when every request was a page render. It stopped being so
# once Teams arrived: a chat tab polls /teams/api/sync every couple of
# seconds, and a chat attachment streams up to TEAMS_ATTACHMENT_MAX_MB
# through a worker - one 25 MB upload on a slow line would hold half the
# server for its entire duration, and every poll behind it would queue.
#
# Threads are the right lever here rather than gevent: this workload is
# I/O-bound on Postgres and R2, and gthread needs no monkey-patching and no
# audit of blocking calls. 2 x 4 = 8 concurrent requests.
worker_class = "gthread"
threads = 4

timeout = 120


def on_starting(server):
    """Apply pending database migrations once, before workers fork.

    The Procfile launches `gunicorn wsgi:app` with no separate migrate
    step, and the models reference columns that only exist after
    `flask db upgrade` - so serving traffic before migrating would 500
    every page. Running here (master process, exactly once) keeps
    additive migrations in lock step with the code they ship with,
    without a per-worker race. Fails loudly so a bad migration stops the
    rollout instead of serving broken pages.
    """
    try:
        from app import create_app
        from flask_migrate import upgrade

        app = create_app()
        with app.app_context():
            upgrade()

        server.log.info("CypherCrew: database migrations applied (at head).")
    except Exception as exc:
        server.log.error("CypherCrew: startup migration failed: %s", exc)
        raise
