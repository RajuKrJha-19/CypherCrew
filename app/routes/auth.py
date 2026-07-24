from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, current_app,
)
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.models import User
from app.extensions import login_manager, db
from app.utils.email import send_email, email_enabled


auth_bp = Blueprint(
    "auth",
    __name__,
    url_prefix="/auth"
)

#: Reset links expire after this many seconds.
RESET_TOKEN_MAX_AGE = 3600


def _reset_serializer():
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"],
        salt="password-reset",
    )


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():

    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(
            email=email
        ).first()

        if not user:
            flash("Invalid email or password.", "error")
            return redirect(url_for("auth.login"))

        if user.status != "active":
            flash("Your account is inactive.", "error")
            return redirect(url_for("auth.login"))

        if not check_password_hash(
            user.password_hash,
            password
        ):
            flash("Invalid email or password.", "error")
            return redirect(url_for("auth.login"))

        login_user(user)

        return redirect(url_for("dashboard.index"))

    return render_template("auth/login.html")


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()

        if user and user.status == "active":
            token = _reset_serializer().dumps(str(user.id))
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

            # Dev convenience: with no SMTP configured, surface the link in
            # the server log so the flow can still be exercised.
            if not email_enabled():
                current_app.logger.info(
                    "Password reset link for %s: %s", user.email, reset_url
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
        user_id = _reset_serializer().loads(token, max_age=RESET_TOKEN_MAX_AGE)
    except SignatureExpired:
        flash("This reset link has expired. Please request a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    except BadSignature:
        flash("This reset link is invalid.", "error")
        return redirect(url_for("auth.forgot_password"))

    user = User.query.get(int(user_id))

    if not user or user.status != "active":
        flash("This reset link is no longer valid.", "error")
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


@auth_bp.route("/logout")
@login_required
def logout():

    logout_user()

    return redirect(url_for("auth.login"))