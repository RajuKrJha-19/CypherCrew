"""OAuth connect + callback routes for the Social Publishing Engine.

Registered only when SOCIAL_ENGINE_ENABLED. The callback validates the
single-use state (CSRF) before exchanging the code; tokens are encrypted by
AccountManager before they touch the database.
"""

from flask import (
    Blueprint, abort, current_app, flash, redirect, request, url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.social.errors import SocialError
from app.social.oauth.manager import OAuthManager
from app.social.services.accounts import AccountManager
from app.utils.permissions import has_permission


oauth_bp = Blueprint("oauth", __name__, url_prefix="/oauth")


def _connect_guard():
    if not has_permission(current_user, "connect_social_accounts"):
        abort(403)


def _redirect_uri(platform):
    base = (
        current_app.config.get("SOCIAL_PUBLIC_BASE_URL")
        or request.url_root.rstrip("/")
    )
    return f"{base}/oauth/{platform}/callback"


@oauth_bp.route("/<platform>/connect")
@login_required
def connect(platform):
    _connect_guard()
    try:
        url = OAuthManager.start(
            platform, _redirect_uri(platform), current_user.id
        )
    except SocialError as exc:
        flash(str(exc), "error")
        return redirect(url_for("social.index"))
    return redirect(url)


@oauth_bp.route("/<platform>/callback")
@login_required
def callback(platform):
    _connect_guard()

    if request.args.get("error"):
        flash(
            "Authorization was cancelled or denied: "
            + request.args.get("error_description", request.args["error"]),
            "error",
        )
        return redirect(url_for("social.index"))

    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        flash("Missing authorization code or state.", "error")
        return redirect(url_for("social.index"))

    try:
        bundle, accounts = OAuthManager.finish(platform, code, state)
        for info in accounts:
            AccountManager.upsert_from_oauth(
                platform, info, bundle, current_user.id
            )
        db.session.commit()
        flash(f"Connected {len(accounts)} {platform} account(s).", "success")
    except SocialError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception("OAuth callback failed for %s", platform)
        flash("Could not complete the connection. Please try again.", "error")

    return redirect(url_for("social.index"))
