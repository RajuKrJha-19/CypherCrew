import calendar
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import User, Task
from app.utils import task_status


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

    search = request.args.get("q", "").strip()
    selected_role = request.args.get("role", "").strip()
    selected_status = request.args.get("status", "").strip()

    query = User.query

    if search:

        like = f"%{search}%"

        query = query.filter(
            db.or_(
                User.name.ilike(like),
                User.email.ilike(like),
                User.designation.ilike(like),
            )
        )

    if selected_role:
        query = query.filter(User.role == selected_role)

    if selected_status:
        query = query.filter(User.status == selected_status)

    sort = request.args.get("sort", "newest").strip()
    sort_options = {
        "newest": User.id.desc(),
        "oldest": User.id.asc(),
        "name_asc": User.name.asc(),
        "name_desc": User.name.desc(),
    }
    if sort not in sort_options:
        sort = "newest"

    page = request.args.get("page", 1, type=int)

    pagination = query.order_by(
        sort_options[sort]
    ).paginate(
        page=page,
        per_page=25,
        error_out=False
    )

    is_filtered = bool(search or selected_role or selected_status)

    return render_template(
        "users/list.html",
        users=pagination.items,
        pagination=pagination,
        search=search,
        selected_role=selected_role,
        selected_status=selected_status,
        sort=sort,
        is_filtered=is_filtered
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
    # Table-only controls: they narrow/reorder the task list below without
    # touching the month KPIs, which stay whole-month totals.
    selected_status = request.args.get("status", "").strip()
    sort = request.args.get("sort", "newest").strip()

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
        Task.status == "Assigned"
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
        Task.status.in_(["Assigned", "In Progress"])
    ).count()

    completion_rate = round(
        (completed_tasks / total_assigned) * 100,
        1
    ) if total_assigned else 0

    # The task table is a drill-down into the selected month: optionally
    # filtered by status and reordered, so a manager can jump straight to,
    # say, this month's overdue work instead of scanning a fixed list.
    table_query = base_query

    if selected_status:
        table_query = table_query.filter(Task.status == selected_status)

    sort_options = {
        "newest": Task.id.desc(),
        "oldest": Task.id.asc(),
        "deadline_asc": Task.deadline.asc(),
        "deadline_desc": Task.deadline.desc(),
        "priority": db.case(
            {"High": 0, "Medium": 1, "Low": 2},
            value=Task.priority,
            else_=3,
        ),
    }

    if sort not in sort_options:
        sort = "newest"

    recent_tasks = table_query.order_by(
        sort_options[sort]
    ).limit(50).all()

    # Dropdown data for the filter bar.
    months = [(i, calendar.month_name[i]) for i in range(1, 13)]
    years = list(range(now.year, now.year - 5, -1))
    if selected_year not in years:
        years.append(selected_year)
        years.sort(reverse=True)

    return render_template(
        "users/performance.html",
        user=user,
        selected_month=selected_month,
        selected_year=selected_year,
        selected_status=selected_status,
        sort=sort,
        months=months,
        years=years,
        statuses=task_status.ALL_STATUSES,
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