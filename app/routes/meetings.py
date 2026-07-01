from datetime import datetime

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash
)

from flask_login import login_required

from app.extensions import db
from app.models import (
    Meeting,
    Client
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

        if not title or not meeting_date:
            flash("Meeting title and date are required.", "error")
            return redirect(
                url_for("meetings.list_meetings")
            )

        meeting = Meeting(
            title=title,
            client_id=int(client_id) if client_id else None,
            meeting_date=datetime.strptime(
                meeting_date,
                "%Y-%m-%dT%H:%M"
            ),
            agenda=agenda
        )

        db.session.add(meeting)
        db.session.commit()

        flash(
            "Meeting created successfully.",
            "success"
        )

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

    return render_template(
        "meetings/list.html",
        meetings=meetings,
        clients=clients
    )


@meetings_bp.route(
    "/<int:meeting_id>/delete",
    methods=["POST"]
)
@login_required
def delete_meeting(meeting_id):

    meeting = Meeting.query.get_or_404(
        meeting_id
    )

    db.session.delete(
        meeting
    )

    db.session.commit()

    flash(
        "Meeting deleted successfully.",
        "success"
    )

    return redirect(
        url_for(
            "meetings.list_meetings"
        )
    )