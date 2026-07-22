from datetime import datetime, timedelta

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash
)

from flask_login import login_required, current_user

from app.extensions import db

from app.models import (
    Meeting,
    Client,
    User,
    Notification
)


meetings_bp = Blueprint(
    "meetings",
    __name__,
    url_prefix="/meetings"
)


@meetings_bp.route("/", methods=["GET", "POST"])
@login_required
def list_meetings():

    if request.method == "POST":

        title = request.form.get("title")
        client_id = request.form.get("client_id")
        meeting_date = request.form.get("meeting_date")
        agenda = request.form.get("agenda")
        employee_ids = request.form.getlist("employee_ids")

        if not title or not meeting_date:
            flash("Meeting title and date are required.", "error")
            return redirect(url_for("meetings.list_meetings"))

        try:
            parsed_client_id = int(client_id) if client_id else None
            parsed_meeting_date = datetime.strptime(
                meeting_date,
                "%Y-%m-%dT%H:%M"
            )
        except ValueError:
            flash("Please select a valid client and meeting date.", "error")
            return redirect(url_for("meetings.list_meetings"))

        meeting = Meeting(
            title=title,
            client_id=parsed_client_id,
            meeting_date=parsed_meeting_date,
            agenda=agenda
        )

        db.session.add(meeting)
        db.session.flush()

        selected_users = []

        if employee_ids:
            selected_users = User.query.filter(
                User.id.in_(employee_ids),
                User.status == "active"
            ).all()

            meeting.participants.extend(selected_users)

            for user in selected_users:
                notification = Notification(
                    user_id=user.id,
                    actor_id=current_user.id,
                    task_id=None,
                    title="New Meeting Assigned",
                    message=(
                        f"{meeting.title} scheduled on "
                        f"{meeting.meeting_date.strftime('%d %b %Y %I:%M %p')}"
                    ),
                    link=url_for(
                        "meetings.meeting_detail",
                        meeting_id=meeting.id
                    )
                )

                db.session.add(notification)

        db.session.commit()

        flash("Meeting created successfully.", "success")

        return redirect(
            url_for(
                "calendar.index",
                year=meeting.meeting_date.year,
                month=meeting.meeting_date.month,
                day=meeting.meeting_date.day
            )
        )

    meetings = Meeting.query.order_by(
        Meeting.meeting_date.asc()
    ).all()

    clients = Client.query.filter_by(
        status="active"
    ).order_by(
        Client.client_name.asc()
    ).all()

    employees = User.query.filter(
        User.status == "active",
        User.role.in_(["super_admin", "admin", "employee"])
    ).order_by(
        User.name.asc()
    ).all()

    return render_template(
        "meetings/list.html",
        meetings=meetings,
        clients=clients,
        employees=employees
    )


@meetings_bp.route("/<int:meeting_id>")
@login_required
def meeting_detail(meeting_id):

    meeting = Meeting.query.get_or_404(meeting_id)

    return render_template(
        "meetings/detail.html",
        meeting=meeting,
        timedelta=timedelta
    )


@meetings_bp.route("/<int:meeting_id>/delete", methods=["POST"])
@login_required
def delete_meeting(meeting_id):

    meeting = Meeting.query.get_or_404(meeting_id)

    db.session.delete(meeting)
    db.session.commit()

    flash("Meeting deleted successfully.", "success")

    return redirect(url_for("meetings.list_meetings"))