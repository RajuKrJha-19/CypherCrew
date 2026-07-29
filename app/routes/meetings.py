from datetime import datetime, timedelta

from flask import (
    Blueprint,
    current_app,
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
from app.utils.permissions import has_permission
from app.utils.permissions import can_manage_meetings as _can_manage_meetings
from app.utils import roles
from app.utils.timezone import ist_now


meetings_bp = Blueprint(
    "meetings",
    __name__,
    url_prefix="/meetings"
)


def can_manage_meetings():
    # The rule itself lives in utils/permissions with every other
    # capability - this used to be "manage_tasks OR admin", which quietly
    # handed people-operations powers to anyone made a team lead.
    return _can_manage_meetings(current_user)


def _teams_owns_meetings():
    """Whether Cypher-Teams has taken this module over.

    When it has, these routes become redirects rather than being deleted.
    The endpoint NAMES have to survive: calendar/index.html builds four
    links with url_for('meetings.meeting_detail'), and removing the
    endpoint would break the calendar for a cosmetic tidy-up. With the flag
    off the pages render exactly as they always did.
    """
    return bool(current_app.config.get("TEAMS_ENABLED"))


@meetings_bp.route("/", methods=["GET", "POST"])
@login_required
def list_meetings():

    if _teams_owns_meetings() and request.method == "GET":
        return redirect(url_for("teams.meetings"))

    if request.method == "POST":

        if not can_manage_meetings():
            flash("You are not allowed to schedule meetings.", "error")
            return redirect(url_for("meetings.list_meetings"))

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

    selected_period = request.args.get("period", "").strip()
    selected_client = request.args.get("client", "").strip()
    selected_member = request.args.get("member", "").strip()
    sort = request.args.get("sort", "date_desc").strip()

    now = ist_now()
    query = Meeting.query

    if selected_period == "upcoming":
        query = query.filter(Meeting.meeting_date >= now)
    elif selected_period == "past":
        query = query.filter(Meeting.meeting_date < now)

    if selected_client.isdigit():
        query = query.filter(Meeting.client_id == int(selected_client))

    if selected_member.isdigit():
        query = query.filter(
            Meeting.participants.any(User.id == int(selected_member))
        )

    if sort == "date_asc":
        query = query.order_by(Meeting.meeting_date.asc())
    else:
        sort = "date_desc"
        query = query.order_by(Meeting.meeting_date.desc())

    meetings = query.all()

    clients = Client.query.filter_by(
        status="active"
    ).order_by(
        Client.client_name.asc()
    ).all()

    employees = User.query.filter(
        User.status == "active",
        User.role.in_(roles.ALL_ROLE_VALUES)
    ).order_by(
        User.name.asc()
    ).all()

    return render_template(
        "meetings/list.html",
        meetings=meetings,
        clients=clients,
        employees=employees,
        selected_period=selected_period,
        selected_client=selected_client,
        selected_member=selected_member,
        sort=sort,
        is_filtered=bool(selected_period or selected_client or selected_member),
    )


@meetings_bp.route("/<int:meeting_id>")
@login_required
def meeting_detail(meeting_id):

    meeting = Meeting.query.get_or_404(meeting_id)

    if _teams_owns_meetings():
        # Teams' own detail page can actually join the call; this one only
        # ever described it.
        return redirect(url_for("teams.meeting_detail", meeting_id=meeting.id))

    return render_template(
        "meetings/detail.html",
        meeting=meeting,
        timedelta=timedelta
    )


@meetings_bp.route("/<int:meeting_id>/delete", methods=["POST"])
@login_required
def delete_meeting(meeting_id):

    if not can_manage_meetings():
        flash("You are not allowed to delete meetings.", "error")
        return redirect(url_for("meetings.list_meetings"))

    meeting = Meeting.query.get_or_404(meeting_id)

    db.session.delete(meeting)
    db.session.commit()

    flash("Meeting deleted successfully.", "success")

    return redirect(url_for("meetings.list_meetings"))