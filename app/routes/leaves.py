from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from app.extensions import db
from app.models import Leave, User


leaves_bp = Blueprint(
    "leaves",
    __name__,
    url_prefix="/leaves"
)


@leaves_bp.route("/", methods=["GET", "POST"])
@login_required
def list_leaves():

    users = User.query.filter(
        User.status == "active",
        User.role.in_(["super_admin", "admin", "employee"])
    ).order_by(User.name.asc()).all()

    if request.method == "POST":

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

    leaves = Leave.query.order_by(
        Leave.start_date.asc()
    ).all()

    return render_template(
        "leaves/list.html",
        leaves=leaves,
        users=users
    )


@leaves_bp.route("/<int:leave_id>/delete", methods=["POST"])
@login_required
def delete_leave(leave_id):

    leave = Leave.query.get_or_404(leave_id)

    db.session.delete(leave)
    db.session.commit()

    flash("Leave deleted successfully.", "success")
    return redirect(url_for("leaves.list_leaves"))