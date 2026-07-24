from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models import Leave, User
from app.utils.permissions import has_permission
from app.utils.timezone import ist_now


leaves_bp = Blueprint(
    "leaves",
    __name__,
    url_prefix="/leaves"
)


def can_manage_leaves():
    return (
        has_permission(current_user, "manage_tasks")
        or current_user.role in ["admin", "super_admin"]
    )


@leaves_bp.route("/", methods=["GET", "POST"])
@login_required
def list_leaves():

    users = User.query.filter(
        User.status == "active",
        User.role.in_(["super_admin", "admin", "employee"])
    ).order_by(User.name.asc()).all()

    if request.method == "POST":

        if not can_manage_leaves():
            flash("You are not allowed to manage employee leaves.", "error")
            return redirect(url_for("leaves.list_leaves"))

        user_id = request.form.get("user_id")
        start_date = request.form.get("start_date")
        end_date = request.form.get("end_date")
        reason = request.form.get("reason")

        if not user_id or not start_date or not end_date:
            flash("Employee, start date and end date are required.", "error")
            return redirect(url_for("leaves.list_leaves"))

        try:
            parsed_user_id = int(user_id)
            parsed_start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
            parsed_end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            flash("Please select a valid employee and valid dates.", "error")
            return redirect(url_for("leaves.list_leaves"))

        leave = Leave(
            user_id=parsed_user_id,
            start_date=parsed_start_date,
            end_date=parsed_end_date,
            reason=reason
        )

        if leave.end_date < leave.start_date:
            flash("End date cannot be before start date.", "error")
            return redirect(url_for("leaves.list_leaves"))

        db.session.add(leave)
        db.session.commit()

        flash("Leave added successfully.", "success")

        return redirect(
            url_for(
                "calendar.index",
                year=leave.start_date.year,
                month=leave.start_date.month,
                day=leave.start_date.day
            )
        )

    # Filters + sort. Leaves have no approval status, so the useful
    # cuts are "whose" and "when": a member, and where the leave sits
    # relative to today.
    selected_member = request.args.get("member", "").strip()
    selected_period = request.args.get("period", "").strip()
    sort = request.args.get("sort", "start_desc").strip()

    today = ist_now().date()
    query = Leave.query

    if selected_member.isdigit():
        query = query.filter(Leave.user_id == int(selected_member))

    if selected_period == "upcoming":
        query = query.filter(Leave.start_date > today)
    elif selected_period == "ongoing":
        query = query.filter(Leave.start_date <= today, Leave.end_date >= today)
    elif selected_period == "past":
        query = query.filter(Leave.end_date < today)

    if sort == "start_asc":
        query = query.order_by(Leave.start_date.asc())
    else:
        sort = "start_desc"
        query = query.order_by(Leave.start_date.desc())

    leaves = query.all()

    return render_template(
        "leaves/list.html",
        leaves=leaves,
        users=users,
        selected_member=selected_member,
        selected_period=selected_period,
        sort=sort,
        is_filtered=bool(selected_member or selected_period),
    )


@leaves_bp.route("/<int:leave_id>/delete", methods=["POST"])
@login_required
def delete_leave(leave_id):

    if not can_manage_leaves():
        flash("You are not allowed to manage employee leaves.", "error")
        return redirect(url_for("leaves.list_leaves"))

    leave = Leave.query.get_or_404(leave_id)

    db.session.delete(leave)
    db.session.commit()

    flash("Leave deleted successfully.", "success")
    return redirect(url_for("leaves.list_leaves"))