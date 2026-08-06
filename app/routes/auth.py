from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, current_app,
    session,
)
from flask_limiter.util import get_remote_address
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.models import User
from app.models.user import password_fingerprint
from app.extensions import login_manager, db, limiter
from app.utils.email import send_email, email_enabled


auth_bp = Blueprint(
    "auth",
    __name__,
    url_prefix="/auth"
)

#: Reset links expire after this many seconds.
RESET_TOKEN_MAX_AGE = 3600

#: A real password hash to compare against when the submitted email doesn't
#: exist, so login takes the same time and returns the same message whether
#: the account is missing or the password is simply wrong (no enumeration).
_DUMMY_PW_HASH = generate_password_hash("cyphercrew-nonexistent-account")


def _pw_fingerprint(user):
    """A short, stable fingerprint of the user's current password hash.

    Embedding it in the reset token makes the link single-use: the instant
    the password changes (via this reset or any other), the hash - and so
    this fingerprint - changes, and every previously-issued link stops
    validating. Closes the replay window where a captured link kept working
    for the full hour even after the user already used it.

    The digest itself now lives on the model, because User.get_id builds the
    session identity from the same value - one definition, so a reset link and
    an open session can never disagree about which password is current.
    """
    return password_fingerprint(user.password_hash)


def _email_rate_key():
    """Throttle by the submitted email, not the client IP.

    The app sits behind a proxy (Cloudflare), so every request can share
    one source IP - IP-keying would either lump all users together or need
    fragile proxy config. Keying on the email throttles per-account guessing
    (the actual brute-force surface) and can never lock everyone out at
    once. Falls back to IP only when no email was posted.
    """
    email = (request.form.get("email", "") or "").strip().lower()
    return email or get_remote_address()


def _reset_serializer():
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"],
        salt="password-reset",
    )


@login_manager.user_loader
def load_user(user_id):
    """Resolve the signed-in user for each request. A deactivated account is
    rejected here (returns None -> anonymous), so 'Inactive' actually cuts
    access immediately for a still-open session, not only at the next login.
    Role/permission changes already take effect per-request because the
    permission rows are re-read; status was the one gap.

    The identity is now `<id>|<password fingerprint>` (see User.get_id), so a
    session opened under an older password is refused here too. A cookie from
    before this shipped carries a bare id, partitions to an empty fingerprint
    and fails the comparison - i.e. everyone signs in once more after deploy,
    which is the correct outcome for a change whose whole point is that old
    sessions must not survive."""
    raw_id, _, fingerprint = str(user_id).partition("|")

    try:
        user = User.query.get(int(raw_id))
    except (TypeError, ValueError):
        return None

    if user is None or user.status != "active":
        return None

    if fingerprint != password_fingerprint(user.password_hash):
        return None

    return user


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit(
    "10 per 5 minutes",
    key_func=_email_rate_key,
    methods=["POST"],
    error_message=(
        "Too many login attempts. Please wait a few minutes and try again."
    ),
)
def login():

    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(
            email=email
        ).first()

        # Always run exactly one password-hash comparison - against the real
        # user, or a dummy hash when the email is unknown - so a missing
        # account and a wrong password take the same time and return the
        # same message. That removes the timing / early-return signal an
        # attacker could use to enumerate which emails are registered.
        if user:
            password_ok = check_password_hash(user.password_hash, password)
        else:
            check_password_hash(_DUMMY_PW_HASH, password)
            password_ok = False

        if not user or not password_ok:
            flash("Invalid email or password.", "error")
            return redirect(url_for("auth.login"))

        if user.status != "active":
            flash("Your account is inactive.", "error")
            return redirect(url_for("auth.login"))

        # Fresh session per sign-in. Anything a pre-login request left behind
        # (flash state, a next-url, or a value an attacker managed to plant)
        # is dropped rather than inherited by the authenticated session.
        session.clear()
        login_user(user)

        return redirect(url_for("dashboard.index"))

    return render_template("auth/login.html")


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit(
    "5 per 15 minutes",
    key_func=_email_rate_key,
    methods=["POST"],
    error_message=(
        "Too many reset requests. Please wait a few minutes and try again."
    ),
)
def forgot_password():

    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()

        if user and user.status == "active":
            token = _reset_serializer().dumps(
                {"uid": user.id, "fp": _pw_fingerprint(user)}
            )
            reset_url = url_for("auth.reset_password", token=token, _external=True)

            send_email(
                to=user.email,
                subject="Reset your CypherCrew password",
                body_text=(
                    f"Hi {user.name},\n\n"
                    "We received a request to reset your CypherCrew password. "
                    "Use the link below within 1 hour to set a new one:\n\n"
                    f"{reset_url}\n\n"
                    "If you didn't request this, you can safely ignore this email."
                ),
                body_html=render_template(
                    "email/password_reset.html", user=user, reset_url=reset_url
                ),
            )

            # Dev convenience: with no SMTP configured, surface the link in the
            # server log so the flow can still be exercised — but ONLY in debug.
            # The URL embeds a live single-use reset token; logging it on a
            # production instance that never finished SMTP setup would spill
            # account-takeover tokens to anyone with log access.
            if not email_enabled():
                if current_app.debug:
                    current_app.logger.info(
                        "Password reset link for %s: %s", user.email, reset_url
                    )
                else:
                    current_app.logger.warning(
                        "Password reset requested for %s but email is not "
                        "configured; the link was NOT logged. Configure SMTP.",
                        user.email,
                    )

        # Same response whether or not the email exists - don't leak which
        # addresses are registered.
        flash(
            "If that email is registered, we've sent a password reset link.",
            "success",
        )
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html")


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):

    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    try:
        data = _reset_serializer().loads(token, max_age=RESET_TOKEN_MAX_AGE)
    except SignatureExpired:
        flash("This reset link has expired. Please request a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    except BadSignature:
        flash("This reset link is invalid.", "error")
        return redirect(url_for("auth.forgot_password"))

    # New tokens are {uid, fp}; tolerate the old bare-id form for any link
    # issued just before this change (they expire within the hour anyway).
    if isinstance(data, dict):
        user_id = data.get("uid")
        token_fp = data.get("fp")
    else:
        user_id = data
        token_fp = None

    user = User.query.get(int(user_id)) if user_id is not None else None

    if not user or user.status != "active":
        flash("This reset link is no longer valid.", "error")
        return redirect(url_for("auth.forgot_password"))

    # Single-use: the fingerprint in the link must still match the account's
    # current password hash. Once the password has been changed (by this
    # link or otherwise), it won't - so a used or superseded link is dead.
    if token_fp is not None and token_fp != _pw_fingerprint(user):
        flash(
            "This reset link has already been used or is no longer valid. "
            "Please request a new one.",
            "error",
        )
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":

        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("auth.reset_password", token=token))

        if password != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("auth.reset_password", token=token))

        user.password_hash = generate_password_hash(password)
        db.session.commit()

        flash("Password updated. Please log in with your new password.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", token=token, user=user)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    # POST (not GET) so a cross-site <img src=".../logout"> can't forcibly
    # sign a user out, and CSRF-protected like every other state change.
    logout_user()

    return redirect(url_for("auth.login"))