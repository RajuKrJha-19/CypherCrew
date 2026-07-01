from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import User, Task


users_bp = Blueprint(
    "users",
    __name__,
    url_prefix="/users"
)


def can_manage_users():
    return current_user.role in ["super_admin", "admin"]


@users_bp.route("/")
@login_required
def list_users():

    if not can_manage_users():
        return redirect(url_for("dashboard.index"))

    users = User.query.order_by(
        User.id.desc()
    ).all()

    return render_template(
        "users/list.html",
        users=users
    )


@users_bp.route("/<int:user_id>/performance")
@login_required
def user_performance(user_id):

    if not can_manage_users():
        return redirect(url_for("dashboard.index"))

    user = User.query.get_or_404(user_id)

    if current_user.role == "admin" and user.role != "employee":
        flash("Admin can view only employee performance.", "error")
        return redirect(url_for("users.list_users"))

    now = datetime.utcnow()

    selected_month = request.args.get("month", now.month, type=int)
    selected_year = request.args.get("year", now.year, type=int)

    base_query = Task.query.filter(
        Task.assigned_to_id == user.id,
        db.extract("month", Task.created_at) == selected_month,
        db.extract("year", Task.created_at) == selected_year
    )

    total_assigned = base_query.count()

    # Employee Completed means task was submitted for review at least once.
    completed_tasks = base_query.filter(
        Task.employee_completed == True
    ).count()

    pending_tasks = base_query.filter(
        Task.status == "Pending"
    ).count()

    in_progress_tasks = base_query.filter(
        Task.status == "In Progress"
    ).count()

    in_review_tasks = base_query.filter(
        Task.status.in_(["Core Review", "Client Review"])
    ).count()

    published_tasks = base_query.filter(
        Task.status == "Published"
    ).count()

    overdue_tasks = base_query.filter(
        Task.deadline < now,
        Task.status.in_(["Pending", "In Progress"])
    ).count()

    completion_rate = round(
        (completed_tasks / total_assigned) * 100,
        1
    ) if total_assigned else 0

    recent_tasks = base_query.order_by(
        Task.id.desc()
    ).limit(8).all()

    return render_template(
        "users/performance.html",
        user=user,
        selected_month=selected_month,
        selected_year=selected_year,
        total_assigned=total_assigned,
        completed_tasks=completed_tasks,
        pending_tasks=pending_tasks,
        in_progress_tasks=in_progress_tasks,
        in_review_tasks=in_review_tasks,
        published_tasks=published_tasks,
        overdue_tasks=overdue_tasks,
        completion_rate=completion_rate,
        recent_tasks=recent_tasks
    )


@users_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_user():

    if not can_manage_users():
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role")
        designation = request.form.get("designation", "").strip()
        status = request.form.get("status")

        if current_user.role == "admin" and role != "employee":
            flash("Admin can create only employee accounts.", "error")
            return redirect(url_for("users.add_user"))

        existing_user = User.query.filter_by(
            email=email
        ).first()

        if existing_user:
            flash("User with this email already exists.", "error")
            return redirect(url_for("users.add_user"))

        user = User(
            name=name,
            email=email,
            phone=phone,
            password_hash=generate_password_hash(password),
            role=role,
            designation=designation,
            status=status
        )

        db.session.add(user)
        db.session.commit()

        flash("User created successfully.", "success")

        return redirect(url_for("users.list_users"))

    return render_template("users/add.html")


@users_bp.route("/edit/<int:user_id>", methods=["GET", "POST"])
@login_required
def edit_user(user_id):

    if not can_manage_users():
        return redirect(url_for("dashboard.index"))

    user = User.query.get_or_404(user_id)

    if user.id == current_user.id and current_user.role == "super_admin":
        if request.method == "POST" and request.form.get("role") != "super_admin":
            flash("You cannot downgrade your own Super Admin role.", "error")
            return redirect(url_for("users.list_users"))

    if current_user.role == "admin" and user.role != "employee":
        flash("Admin can edit only employee accounts.", "error")
        return redirect(url_for("users.list_users"))

    if request.method == "POST":

        user.name = request.form.get("name", "").strip()
        user.phone = request.form.get("phone", "").strip()
        user.designation = request.form.get("designation", "").strip()
        user.status = request.form.get("status")

        if current_user.role == "super_admin":
            user.role = request.form.get("role")

        password = request.form.get("password", "")

        if password:
            user.password_hash = generate_password_hash(password)

        db.session.commit()

        flash("User updated successfully.", "success")

        return redirect(url_for("users.list_users"))

    return render_template(
        "users/edit.html",
        user=user
    )