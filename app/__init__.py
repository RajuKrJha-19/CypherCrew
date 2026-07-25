import os

from flask import Flask
from config import Config
from app.extensions import (
    csrf,
    db,
    limiter,
    login_manager,
    migrate,
)
from app.seed import seed_database
from app.observability import init_logging, init_sentry, register_health
from app.utils.text_filters import linkify_text

def create_app():

    app = Flask(__name__)

    
    app.config.from_object(Config)

    # Observability first, so anything that fails during the rest of
    # startup is logged (and reported) rather than lost.
    init_logging(app)
    init_sentry(app)
    register_health(app)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    limiter.init_app(app)

    # CSRF protection for every state-changing request. The token has no
    # hard expiry (None) so a long-open dashboard tab never fails a submit
    # with a stale token - it stays valid for the life of the session,
    # which is the right lifetime for an internal tool people leave open
    # all day. Tokens are attached automatically client-side (see
    # static/js/csrf.js): a fetch wrapper adds the X-CSRFToken header and a
    # submit-time injector adds the hidden field, so no per-form template
    # change was needed.
    app.config.setdefault("WTF_CSRF_TIME_LIMIT", None)
    csrf.init_app(app)

    from werkzeug.exceptions import RequestEntityTooLarge

    @app.errorhandler(RequestEntityTooLarge)
    def _upload_too_large(error):
        from flask import flash, jsonify, redirect, request, url_for

        limit_mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        message = (
            f"That upload is too large (limit {limit_mb} MB per request). "
            "For big video files, use the submission uploader, which sends "
            "them in parts."
        )
        wants_json = (
            request.is_json
            or "application/json" in (request.headers.get("Accept") or "")
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )
        if wants_json:
            return jsonify(error="too_large", message=message), 413

        flash(message, "error")
        return redirect(request.referrer or url_for("dashboard.index"))

    # Branded error pages. They extend the minimal base.html (not the app
    # shell) and touch no database, so they render safely even when the
    # request that failed left the session in a bad state.
    from flask import render_template

    @app.errorhandler(403)
    def _forbidden(error):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def _page_not_found(error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def _internal_error(error):
        # Flask already logs the traceback via app.logger; just present a
        # calm page rather than a raw stack trace.
        return render_template("errors/500.html"), 500

    from flask_wtf.csrf import CSRFError

    @app.errorhandler(CSRFError)
    def _csrf_error(error):
        from flask import flash, jsonify, redirect, request, url_for

        message = (
            "Your session timed out or the page was open too long. "
            "Please refresh and try again."
        )
        # Fetch/JSON callers get JSON; form posts get a flash + bounce back
        # to where they came from (or the dashboard).
        wants_json = (
            request.is_json
            or "application/json" in (request.headers.get("Accept") or "")
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )
        if wants_json:
            return jsonify(error="csrf", message=message), 400

        flash(message, "error")
        return redirect(request.referrer or url_for("dashboard.index"))

    # Friendly response when a throttle trips: the login form gets a flash
    # + redirect (not a bare 429 page); anything else gets JSON.
    from flask import flash, jsonify, redirect, request, url_for

    @app.errorhandler(429)
    def _rate_limited(error):
        message = (
            getattr(error, "description", None)
            or "Too many attempts. Please wait a few minutes and try again."
        )
        if request.path.startswith("/auth/"):
            flash(message, "error")
            return redirect(url_for("auth.login"))
        return jsonify(error="rate_limited", message=message), 429

    from app import models

    # Newly uploaded images get a thumbnail generated in the background.
    # Registered once, on the session, so every upload path is covered.
    from app.services import thumbnails
    thumbnails.register_events(db.session)
    thumbnails.register_cli(app)

    from app.routes.auth import auth_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.users import users_bp
    from app.routes.permissions import permissions_bp
    from app.routes.clients import clients_bp
    from app.routes.tasks import tasks_bp 
    from app.routes.notes import notes_bp
    from app.routes.reports import reports_bp
    from app.routes.notifications import notifications_bp
    from app.routes.calendar import calendar_bp
    from app.routes.holidays import holidays_bp
    from app.routes.meetings import meetings_bp
    from app.routes.leaves import leaves_bp
    from app.routes.gallery import gallery_bp
    from app.routes.search import search_bp
    from app.routes.profile import profile_bp
    from app.routes.internal import internal_bp


    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(permissions_bp)
    app.register_blueprint(clients_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(calendar_bp)
    app.register_blueprint(holidays_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(leaves_bp)
    app.register_blueprint(gallery_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(internal_bp)

    with app.app_context():
        if app.config.get("AUTO_SEED", True):
            seed_database()

    from app.utils.permissions import has_permission
    from app.utils.greeting import greet, today_label
    from app.utils.avatars import (
        avatar_url,
        file_preview_url,
        file_thumbnail_url,
        file_badge,
    )

    app.jinja_env.globals.update(
        has_permission=has_permission,
        # Registered globally rather than passed from each dashboard
        # route, so all three headers greet the same way.
        greet=greet,
        today_label=today_label,
        # Presigned avatar URL (or None -> initials) for any user, so the
        # top bar, cards and profile pages render pictures the same way.
        avatar_url=avatar_url,
        # Direct presigned URL for a task file (used by video thumbnails).
        file_preview_url=file_preview_url,
        # Direct presigned URL for a file's generated thumbnail (grid tiles
        # point <img> straight at R2 - no redirect hop through the app).
        file_thumbnail_url=file_thumbnail_url,
        # Corner format badge (PS/AI/PDF/...) for a file tile, or None.
        file_badge=file_badge,
    )

    app.jinja_env.filters["linkify"] = linkify_text

    from app.utils.mentions import highlight_mentions
    app.jinja_env.filters["mentions"] = highlight_mentions

    # Cache-bust static assets: append each file's mtime as ?v= so a
    # shipped CSS/JS change is fetched fresh instead of served from a
    # stale browser cache. Without this, a fixed style.css can keep
    # rendering the old layout until the cache happens to expire.
    @app.url_defaults
    def _static_cache_bust(endpoint, values):
        if endpoint == "static" and values.get("filename"):
            try:
                file_path = os.path.join(app.static_folder, values["filename"])
                values["v"] = int(os.stat(file_path).st_mtime)
            except OSError:
                pass

    return app