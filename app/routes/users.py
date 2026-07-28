from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import User, Task
from app.utils import periods, roles, task_status
from app.utils.timezone import ist_date, ist_now
from app.utils.permissions import (
    apply_role_defaults, can_manage_users as _can_manage_users,
)


users_bp = Blueprint(
    "users",
    __name__,
    url_prefix="/users"
)


def can_manage_users():
    """May this person reach the user-administration screens at all?

    Honours the `manage_users` permission now. It always claimed to -
    the sidebar and the command palette both revealed the Users screen to
    anyone holding it - but the routes tested the role instead, so the
    grant produced a link that bounced you straight back to the dashboard.
    """
    return _can_manage_users(current_user)


def may_administer(target):
    """May the signed-in user create, edit or inspect this person?

    Only downwards. The owner may administer anybody; everyone else is
    limited to people who are not administrators themselves, so an admin
    cannot edit a fellow admin's account (or the owner's) and cannot use
    the edit form as a route to their own promotion.

    This replaces three copies of `user.role != "employee"`, which meant
    the same thing back when "employee" was the only non-administrator
    role. With thirteen of them, the question is about tier, not about one
    particular value.
    """
    if roles.is_owner(current_user.role):
        return True

    return not roles.is_management(getattr(target, "role", None))


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


@users_bp.route("/<int:user_id>")
@login_required
def user_detail(user_id):
    """Read-only profile of any user. Your own profile is always viewable
    (employees reach it via the account menu); viewing someone else's needs
    user-management rights."""

    if user_id != current_user.id and not can_manage_users():
        return redirect(url_for("profile.my_profile"))

    user = User.query.get_or_404(user_id)

    return render_template(
        "users/profile.html",
        user=user,
        is_self=(user.id == current_user.id),
    )


@users_bp.route("/<int:user_id>/performance")
@login_required
def user_performance(user_id):

    if not can_manage_users():
        return redirect(url_for("dashboard.index"))

    user = User.query.get_or_404(user_id)

    if not may_administer(user):
        flash("You can only view performance for your own team.", "error")
        return redirect(url_for("users.list_users"))

    # Deadlines are naive IST wall-clock (from a datetime-local input), and
    # every other overdue check in the app compares to ist_now(); using utcnow()
    # here would under-count overdue by up to 5.5h and disagree with them.
    now = ist_now()

    # Same presets as the dashboard's Performance band, plus All time -
    # these figures are plain totals, so an unbounded window is a
    # perfectly good answer here (it isn't on the dashboard, which counts
    # per day and draws vs-previous deltas). All time is the default: it
    # is the honest "how has this person done" view, and every other
    # preset is one click away.
    period = periods.resolve_period(request.args, allow_all=True,
                                    default="all")

    # Table-only controls: they narrow/reorder the task list below without
    # touching the KPI cards, which stay whole-period totals.
    selected_status = request.args.get("status", "").strip()
    sort = request.args.get("sort", "newest").strip()

    base_query = Task.query.filter(Task.assigned_to_id == user.id)

    if not period["is_all_time"]:
        # Inclusive of the end date: the window is a range of days, and
        # `created_at < end` would silently drop everything made on the
        # last day of it.
        base_query = base_query.filter(
            ist_date(Task.created_at) >= period["start"],
            ist_date(Task.created_at) <= period["end"],
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
        Task.status.in_(task_status.OVERDUE_STATUSES)
    ).count()

    completion_rate = round(
        (completed_tasks / total_assigned) * 100,
        1
    ) if total_assigned else 0

    # The task table is a drill-down into the selected window: optionally
    # filtered by status and reordered, so a manager can jump straight to,
    # say, this week's overdue work instead of scanning a fixed list.
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

    return render_template(
        "users/performance.html",
        user=user,
        period=period,
        selected_status=selected_status,
        sort=sort,
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

        # role is NOT NULL and name/email/password are required for a usable
        # account; without this an incomplete submit hit the DB and raised an
        # IntegrityError (500) instead of a clean validation message.
        if not name or not email or not password or not role:
            flash("Name, email, password and role are required.", "error")
            return redirect(url_for("users.add_user"))

        # The dropdown only offers roles this person may hand out, but a
        # form post is just a string - this is what actually stops one.
        # Previously nothing did: any value at all was written straight
        # into the column.
        if not roles.can_assign_role(current_user, role):
            flash("You cannot create an account with that role.", "error")
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
        db.session.flush()

        # The whole point of the role catalog: a new Senior Video Editor
        # can review a junior's work the moment the account exists, rather
        # than after somebody remembers to visit the permissions screen.
        granted = apply_role_defaults(user, granted_by=current_user)

        db.session.commit()

        if granted:
            flash(
                f"User created with the {roles.label(role)} defaults "
                f"({len(granted)} permissions).",
                "success",
            )
        else:
            flash("User created successfully.", "success")

        return redirect(url_for("users.list_users"))

    return render_template("users/add.html")


@users_bp.route("/edit/<int:user_id>", methods=["GET", "POST"])
@login_required
def edit_user(user_id):

    if not can_manage_users():
        return redirect(url_for("dashboard.index"))

    user = User.query.get_or_404(user_id)

    # Nobody changes their own role - not even the owner. It used to be
    # phrased as "the Super Admin may not downgrade themselves", which
    # protected the one account that could not be recreated; the general
    # rule protects the other direction too, where an admin edits their
    # own account into something more powerful.
    is_self = user.id == current_user.id

    if is_self and request.method == "POST" \
            and request.form.get("role") not in (None, user.role):
        flash("You cannot change your own role.", "error")
        return redirect(url_for("users.list_users"))

    if not may_administer(user):
        flash("You can only edit accounts below your own role.", "error")
        return redirect(url_for("users.list_users"))

    if request.method == "POST":

        user.name = request.form.get("name", "").strip()
        user.phone = request.form.get("phone", "").strip()
        user.designation = request.form.get("designation", "").strip()
        user.status = request.form.get("status")

        new_role = request.form.get("role")

        if new_role and new_role != user.role and not is_self:

            if not roles.can_assign_role(current_user, new_role):
                flash("You cannot assign that role.", "error")
                return redirect(url_for("users.edit_user", user_id=user.id))

            user.role = new_role

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