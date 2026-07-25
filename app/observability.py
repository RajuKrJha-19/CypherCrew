"""Production observability - logging, a health check, and an optional
error-monitoring hook.

All of this is additive and behaviour-neutral: it changes nothing about how
requests are handled, it only makes failures *visible*. Before this the team
learned about a production 500 or a down database only when someone reported
it. None of it introduces a required dependency - Sentry is wired but inert
unless both a DSN and the SDK are present.
"""

import logging
import os

from flask import jsonify
from sqlalchemy import text

from app.extensions import db


def init_logging(app):
    """Route app logs to stderr in one consistent, timestamped format so
    gunicorn / systemd capture is actually readable and greppable. Level is
    env-tunable via LOG_LEVEL (default INFO). Idempotent - safe to call on
    every reloader restart without stacking handlers.
    """
    level = getattr(
        logging,
        os.getenv("LOG_LEVEL", "INFO").upper(),
        logging.INFO,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)s in %(name)s: %(message)s"
        )
    )

    app.logger.handlers = [handler]
    app.logger.setLevel(level)
    # Don't also bubble to the root logger, or every line logs twice.
    app.logger.propagate = False


def init_sentry(app):
    """Optional error monitoring. No-ops unless SENTRY_DSN is set AND
    sentry-sdk is installed - so it adds zero *required* dependency. To
    enable in production: `pip install sentry-sdk` and set SENTRY_DSN.
    """
    dsn = os.getenv("SENTRY_DSN")

    if not dsn:
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:
        app.logger.warning(
            "SENTRY_DSN is set but sentry-sdk is not installed; "
            "error monitoring is disabled."
        )
        return

    sentry_sdk.init(
        dsn=dsn,
        integrations=[FlaskIntegration()],
        traces_sample_rate=float(
            os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")
        ),
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
    )
    app.logger.info("Sentry error monitoring enabled.")


def register_health(app):
    """A cheap, unauthenticated liveness + database probe for uptime
    monitors, load balancers and deploy checks. 200 when the DB answers,
    503 otherwise - so "is production up and can it reach Postgres" is a
    single HTTP call instead of waiting for a user to hit a broken page.
    """

    @app.route("/healthz")
    def healthz():
        try:
            db.session.execute(text("SELECT 1"))
            return jsonify(status="ok", database="ok"), 200
        except Exception:
            app.logger.exception(
                "Health check failed - database unreachable."
            )
            return jsonify(status="error", database="unreachable"), 503
