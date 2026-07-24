bind = "0.0.0.0:8000"
workers = 2
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
