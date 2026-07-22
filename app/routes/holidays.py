from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models import Holiday
from app.utils.permissions import has_permission


holidays_bp = Blueprint(
    "holidays",
    __name__,
    url_prefix="/holidays"
)


def can_manage_holidays():
    return (
        has_permission(current_user, "manage_tasks")
        or current_user.role in ["admin", "super_admin"]
    )


@holidays_bp.route("/", methods=["GET", "POST"])
@login_required
def list_holidays():

    if request.method == "POST":

        if not can_manage_holidays():
            flash("You are not allowed to manage holidays.", "error")
            return redirect(url_for("holidays.list_holidays"))

        title = request.form.get("title")
        holiday_date = request.form.get("holiday_date")
        description = request.form.get("description")

        if not title or not holiday_date:
            flash("Holiday title and date are required.", "error")
            return redirect(url_for("holidays.list_holidays"))

        try:
            parsed_date = datetime.strptime(holiday_date, "%Y-%m-%d").date()
        except ValueError:
            flash("Holiday date must be a valid date.", "error")
            return redirect(url_for("holidays.list_holidays"))

        holiday = Holiday(
            title=title,
            holiday_date=parsed_date,
            description=description
        )

        db.session.add(holiday)
        db.session.commit()

        flash("Holiday added successfully.", "success")

        return redirect(
            url_for(
                "calendar.index",
                year=holiday.holiday_date.year,
                month=holiday.holiday_date.month,
                day=holiday.holiday_date.day
            )
        )

    holidays = Holiday.query.order_by(
        Holiday.holiday_date.asc()
    ).all()

    return render_template(
        "holidays/list.html",
        holidays=holidays
    )


@holidays_bp.route("/<int:holiday_id>/delete", methods=["POST"])
@login_required
def delete_holiday(holiday_id):

    if not can_manage_holidays():
        flash("You are not allowed to manage holidays.", "error")
        return redirect(url_for("holidays.list_holidays"))

    holiday = Holiday.query.get_or_404(holiday_id)

    db.session.delete(holiday)
    db.session.commit()

    flash("Holiday deleted successfully.", "success")
    return redirect(url_for("holidays.list_holidays"))