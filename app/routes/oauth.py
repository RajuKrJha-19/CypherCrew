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
from app.social.tokens.vault import VaultDisabled, get_vault
from app.utils.permissions import can_connect_social_accounts
from app.utils.social_platforms import label as platform_label


oauth_bp = Blueprint("oauth", __name__, url_prefix="/oauth")

_VAULT_MISCONFIG = (
    "Social publishing is not fully configured: SOCIAL_TOKEN_KEY is missing, "
    "so account tokens cannot be encrypted. Set it in the environment and "
    "restart, then try connecting again."
)

# Human wording for the "we connected N accounts of platform X" summary.
_ACCOUNT_NOUNS = {
    "facebook": ("Facebook Page", "Facebook Pages"),
    "instagram": ("Instagram account", "Instagram accounts"),
}

# Shown when the Meta consent succeeded but no Instagram account came back -
# almost always because no IG Business/Creator account is linked to the Page
# (or the Instagram permission was declined). Never fail silently.
_NO_INSTAGRAM = (
    "No Instagram Business account is linked to your Facebook Page(s). To "
    "publish to Instagram, link an Instagram Business or Creator account to a "
    "Page you manage (Meta Business settings → Instagram accounts), grant the "
    "Instagram permission, then reconnect."
)


# What each requested-but-not-granted Meta permission costs, so the message
# names the broken feature rather than a raw scope string.
_SCOPE_FEATURES = {
    "pages_show_list": "listing the Pages you manage",
    "pages_read_engagement": "reading your Page's own posts",
    "pages_read_user_content": "the Engage inbox for Facebook comments",
    "pages_manage_posts": "publishing to Facebook Pages",
    "pages_manage_engagement": "replying to Facebook comments and the auto "
                               "first comment",
    "read_insights": "Facebook Page analytics",
    "business_management": "discovering Pages managed through a Business "
                           "Manager (agency-managed client Pages)",
    "instagram_basic": "reading the Instagram profile and posts",
    "instagram_content_publish": "publishing to Instagram",
    "instagram_manage_comments": "the Engage inbox for Instagram comments",
    "instagram_manage_insights": "Instagram analytics",
}


def _ungranted_warning(scopes):
    """Spell out a partial grant. Meta returns a token for the permissions
    it did give and says nothing about the rest, so without this the connect
    flashes plain success and the missing features just quietly do nothing."""
    features = [_SCOPE_FEATURES.get(s, s) for s in scopes]
    return (
        "Connected, but Meta did not grant "
        + str(len(scopes))
        + (" permission" if len(scopes) == 1 else " permissions")
        + ": " + ", ".join(features)
        + ". That part will not work until granted. Re-run Connect and leave "
          "every permission switched on; if a permission is still missing "
          "afterwards, it is pending Meta App Review for this app."
    )


def _account_phrase(platform, count):
    singular, plural = _ACCOUNT_NOUNS.get(
        platform, (f"{platform_label(platform)} account",
                   f"{platform_label(platform)} accounts"))
    return f"{count} {singular if count == 1 else plural}"


def _connect_guard():
    if not can_connect_social_accounts(current_user):
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

    # Discovered-only platforms (Instagram) have no standalone login: they
    # are found through the Facebook consent and refreshed from connected
    # Pages. Send the user to that action instead of an unnecessary OAuth.
    from app.social.registry import get_provider
    provider = get_provider(platform)
    if provider is not None and not getattr(provider, "connectable", True):
        flash(
            "Instagram accounts are connected automatically with Facebook. "
            "Use “Refresh Instagram” to pick up accounts linked to your "
            "Pages.", "info")
        return redirect(url_for("social.accounts"))

    # Fail fast on misconfiguration: without a token vault we cannot store
    # the credentials, so there's no point round-tripping to the provider.
    try:
        get_vault()
    except VaultDisabled:
        current_app.logger.error(
            "[oauth:%s] connect blocked: token vault disabled "
            "(SOCIAL_TOKEN_KEY unset)", platform)
        flash(_VAULT_MISCONFIG, "error")
        return redirect(url_for("social.index"))

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
    log = current_app.logger

    if request.args.get("error"):
        log.warning("[oauth:%s] provider returned error: %s", platform,
                    request.args.get("error"))
        flash(
            "Authorization was cancelled or denied: "
            + request.args.get("error_description", request.args["error"]),
            "error",
        )
        return redirect(url_for("social.index"))

    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        log.warning("[oauth:%s] callback missing code/state", platform)
        flash("Missing authorization code or state.", "error")
        return redirect(url_for("social.index"))

    log.info("[oauth:%s] callback received; starting token exchange + save",
             platform)
    try:
        # finish_all also discovers sibling platforms that share this consent:
        # one Facebook login returns the Facebook Pages AND the Instagram
        # Business accounts linked to them.
        bundle, results, empty = OAuthManager.finish_all(
            platform, code, state, expected_by_id=current_user.id)
        saved = {}
        for plat, info in results:
            # upsert_from_oauth encrypts the (per-Page) token via the vault.
            AccountManager.upsert_from_oauth(
                plat, info, bundle, current_user.id
            )
            saved[plat] = saved.get(plat, 0) + 1
        db.session.commit()
        total = sum(saved.values())
        log.info("[oauth:%s] 4/4 encrypted + saved %d account(s) (%s); done",
                 platform, total,
                 ", ".join(f"{k}={v}" for k, v in saved.items()) or "none")

        ungranted = (bundle.meta or {}).get("ungranted_scopes") or []
        if ungranted:
            log.warning("[oauth:%s] partial grant - Meta withheld: %s",
                        platform, ", ".join(ungranted))

        if total:
            phrases = [_account_phrase(p, n) for p, n in saved.items()]
            flash("Connected " + " and ".join(phrases) + ".", "success")
            # A permission the user thinks they just granted but Meta withheld.
            if ungranted:
                flash(_ungranted_warning(ungranted), "error")
            # Facebook connected but no linked Instagram - explain, don't hide.
            if "instagram" in empty and "facebook" in saved:
                flash(_NO_INSTAGRAM, "info")
        elif "instagram" in empty and "facebook" in empty:
            # Nothing at all came back.
            flash(
                "Authorization succeeded but no publishable account was found. "
                "Ensure you manage at least one Facebook Page (with content "
                "permissions) and, for Instagram, that a Business/Creator "
                "account is linked to it.", "error")
        else:
            flash(
                f"Connected, but no publishable {platform_label(platform)} "
                "account was found.", "error")
    except VaultDisabled as exc:
        db.session.rollback()
        log.error("[oauth:%s] save FAILED: token vault disabled: %s",
                  platform, exc)
        flash(_VAULT_MISCONFIG, "error")
    except SocialError as exc:
        # Provider/OAuth error already classified - show its real message.
        db.session.rollback()
        log.warning("[oauth:%s] connect FAILED: %s", platform, exc)
        flash(f"Could not connect: {exc}", "error")
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("[oauth:%s] connect FAILED with an unexpected error",
                      platform)
        flash("Could not complete the connection — check the server logs "
              "for the exact error.", "error")

    return redirect(url_for("social.index"))
