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
    # Public: Meta's app review fetches these anonymously, and the data
    # deletion callback must work for someone who has already removed the
    # app and can no longer sign in. Registered unconditionally - the legal
    # pages must not disappear when the social engine is switched off.
    from app.routes.legal import legal_bp


    app.register_blueprint(legal_bp)
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

    # Social Publishing Engine. Registered ONLY when the feature flag is on,
    # so with it off the engine's routes/providers are entirely absent and
    # the app behaves exactly as before. (The /internal/social/* cron
    # endpoints live on internal_bp above and self-gate on the flag too.)
    if app.config.get("SOCIAL_ENGINE_ENABLED"):
        from app.social.registry import load_providers
        load_providers(app)

        from app.routes.social import social_bp
        from app.routes.oauth import oauth_bp
        app.register_blueprint(social_bp)
        app.register_blueprint(oauth_bp)

        # Local Graph API emulator - lets the real Meta provider be exercised
        # end-to-end without a real Meta app. Dev/test only.
        if app.config.get("META_EMULATOR"):
            from app.social.providers.meta_emulator import meta_emulator_bp
            app.register_blueprint(meta_emulator_bp)
            # The emulator stands in for graph.facebook.com, which is not
            # behind our CSRF; the provider's server-side POSTs carry no
            # CSRF token, so exempt it (dev-only blueprint).
            csrf.exempt(meta_emulator_bp)
            app.logger.warning(
                "META_EMULATOR is on - Meta provider is talking to the local "
                "Graph emulator, not graph.facebook.com."
            )

        # Fail LOUD (but not fatal) on misconfiguration, so a half-configured
        # engine is obvious in the logs rather than silently broken.
        if not app.config.get("SOCIAL_TOKEN_KEY"):
            app.logger.warning(
                "SOCIAL_ENGINE_ENABLED is on but SOCIAL_TOKEN_KEY is unset - "
                "accounts cannot be connected (token vault disabled)."
            )
        if not app.config.get("SOCIAL_WORKER_TOKEN"):
            app.logger.warning(
                "SOCIAL_ENGINE_ENABLED is on but SOCIAL_WORKER_TOKEN is unset - "
                "the /internal/social/* cron endpoints stay closed (403)."
            )

        # Auto-publishing: a background thread enqueues + drains the queue so
        # scheduled posts go out on time with no external cron. Off in tests.
        if app.config.get("SOCIAL_INPROCESS_WORKER", True):
            from app.social.queue.autoworker import start_background_worker
            start_background_worker(app)

    with app.app_context():
        if app.config.get("AUTO_SEED", True):
            seed_database()

    from app.utils.permissions import (
        has_permission, can_manage_clients, can_manage_social_engine,
        can_use_social, can_connect_social_accounts, can_view_all_tasks,
        can_assign_tasks, can_view_team_performance, can_manage_users,
        can_manage_permissions, can_manage_leaves, can_manage_holidays,
        can_manage_meetings, can_publish, can_review,
    )
    from app.utils import roles as roles_module
    from app.utils.social_platforms import PLATFORM_ICONS, PLATFORM_LABELS
    from app.utils import task_status as task_status_module
    from app.utils.greeting import greet, today_label
    from app.utils.avatars import (
        avatar_url,
        file_preview_url,
        file_thumbnail_url,
        file_badge,
    )

    import os as _os
    from flask import url_for as _url_for

    def _asset_url(filename):
        """static URL + ?v=<file mtime> so a changed asset always cache-busts."""
        try:
            version = int(_os.path.getmtime(
                _os.path.join(app.static_folder, filename)))
        except OSError:
            version = 0
        return _url_for("static", filename=filename, v=version)

    def _social_publish_badge(task):
        """Publish sub-state badge for a task's linked Studio post(s), or None.
        Global so kanban cards, the task list and task detail render the same
        badge without per-route wiring. Cheap + guarded (engine-gated)."""
        if not app.config.get("SOCIAL_ENGINE_ENABLED"):
            return None
        try:
            from app.social.services.task_link import publish_badge
            return publish_badge(task)
        except Exception:  # noqa: BLE001 - a badge must never break a page
            return None

    app.jinja_env.globals.update(
        has_permission=has_permission,
        # Publish-state badge (Draft in Studio / Scheduled / In queue / Live /
        # failed / outside Studio) for a task, derived from its Studio post.
        social_publish_badge=_social_publish_badge,
        # "May operate the Social Publishing engine" (owner/admin only) -
        # gates the internal ops controls (worker kick, retry/requeue) so
        # employees and managers never see engine machinery.
        can_manage_social_engine=can_manage_social_engine,
        # Platform label/icon lookups, from the one catalog in
        # utils/social_platforms. Globals because every Studio template
        # needs them: they used to be a {% set %} literal copy-pasted into
        # a dozen templates, so adding a platform meant editing all twelve
        # and any one that was missed silently rendered a bare key.
        PF_LABEL=PLATFORM_LABELS,
        PF_ICON=PLATFORM_ICONS,
        # "May work in Social Studio" (admin/super_admin, or the explicit
        # manage_social permission) - gates the Social tab itself and the
        # task-detail handoff controls.
        can_use_social=can_use_social,
        # "May connect/disconnect channels" - the Channels half of the tab.
        can_connect_social_accounts=can_connect_social_accounts,
        # "May curate this client" (admin/super_admin, or the explicit
        # manage_clients permission) - distinct from "may read it",
        # which is now every signed-in user. See the helper's docstring.
        can_manage_clients=can_manage_clients,
        # The rest of the capability vocabulary. Globals for the same reason
        # as the ones above: the sidebar, the command palette and the quick-
        # create menu each used to carry their own {% set %} copy of the
        # rule, so a guard change had to be made in four places and the nav
        # drifted out of step with the routes.
        can_view_all_tasks=can_view_all_tasks,
        can_assign_tasks=can_assign_tasks,
        can_view_team_performance=can_view_team_performance,
        can_manage_users=can_manage_users,
        can_manage_permissions=can_manage_permissions,
        can_manage_leaves=can_manage_leaves,
        can_manage_holidays=can_manage_holidays,
        can_manage_meetings=can_manage_meetings,
        can_publish=can_publish,
        can_review=can_review,
        # Role display, from the one catalog in utils/roles. role_label is
        # what makes "super_admin" read as "Owner" everywhere at once,
        # instead of eighteen templates each doing .replace("_"," ")|title.
        role_label=roles_module.label,
        role_badge_class=roles_module.badge_class,
        role_grouped_options=roles_module.grouped_options,
        assignable_roles=roles_module.assignable_by,
        # Status names, groups and descriptions. A global because it is
        # pure constants shared by the dashboards, the task list and the
        # KPI cards - passing it per route meant a partial that used it
        # rendered blank on any dashboard whose route had forgotten to.
        # Routes that pass task_status= explicitly simply shadow this
        # with the identical module.
        task_status=task_status_module,
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
        # Cache-busted static URL: appends ?v=<file mtime> so browsers fetch
        # a fresh copy the instant an asset (e.g. style.css) changes, instead
        # of serving a stale cached one. Use for CSS/JS in the shell.
        asset_url=_asset_url,
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