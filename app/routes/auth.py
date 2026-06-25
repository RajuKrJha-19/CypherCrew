from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash

from app.models import User
from app.extensions import login_manager


auth_bp = Blueprint(
    "auth",
    __name__,
    url_prefix="/auth"
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


@auth_bp.route("/logout")
@login_required
def logout():

    logout_user()

    return redirect(url_for("auth.login"))