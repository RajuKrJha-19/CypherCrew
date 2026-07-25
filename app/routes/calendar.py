import calendar
from datetime import datetime, date, timedelta
from app.utils.timezone import ist_now
from flask import (
    Blueprint,
    render_template,
    request
)

from flask_login import login_required
from app.utils import task_status

from app.models import (
    Task,
    Client,
    User,
    Holiday,
    Meeting
)


calendar_bp = Blueprint(
    "calendar",
    __name__
)


@calendar_bp.route("/calendar")
@login_required
def index():

    today = ist_now()

    month = request.args.get(
        "month",
        default=today.month,
        type=int
    )

    year = request.args.get(
        "year",
        default=today.year,
        type=int
    )

    selected_client = request.args.get(
        "client_id",
        type=int
    )

    selected_employee = request.args.get(
        "employee_id",
        type=int
    )

    selected_status = request.args.get(
        "status",
        "",
        type=str
    ).strip()

    selected_day = request.args.get(
        "day",
        type=int
    )

    if month < 1:
        month = 12
        year -= 1

    elif month > 12:
        month = 1
        year += 1

    # date() only supports years 1..9999, and the calendar's 6-week grid
    # can spill into the neighbouring year, so an out-of-range year (e.g.
    # from a hand-edited URL) would raise ValueError below. Keep a safe
    # margin and fall back to the current year.
    if not (1 < year < 9999):
        year = today.year

    cal = calendar.Calendar(
        firstweekday=6
    )

    month_days = cal.monthdatescalendar(
        year,
        month
    )

    query = Task.query.filter(
        Task.deadline.isnot(None)
    )

    if selected_client:
        query = query.filter(
            Task.client_id == selected_client
        )

    if selected_employee:
        query = query.filter(
            Task.assigned_to_id == selected_employee
        )

    if selected_status:
        query = query.filter(
            Task.status == selected_status
        )

    tasks = query.order_by(
        Task.deadline.asc()
    ).all()

    events = {}

    for task in tasks:
        key = task.deadline.date()

        if key not in events:
            events[key] = []

        events[key].append(task)

    holidays = Holiday.query.order_by(
        Holiday.holiday_date.asc()
    ).all()

    holiday_events = {}

    for holiday in holidays:
        key = holiday.holiday_date

        if key not in holiday_events:
            holiday_events[key] = []

        holiday_events[key].append(holiday)

    # -------------------------
    # Meetings
    # -------------------------

    meetings = Meeting.query.order_by(
        Meeting.meeting_date.asc()
    ).all()

    meeting_events = {}

    for meeting in meetings:
        key = meeting.meeting_date.date()

        if key not in meeting_events:
            meeting_events[key] = []

        meeting_events[key].append(meeting)

    
    selected_date = None
    selected_tasks = []
    selected_holidays = []
    selected_meetings = []

    if selected_day:
        try:
            selected_date = date(
                year,
                month,
                selected_day
            )

            selected_tasks = events.get(
                selected_date,
                []
            )

            selected_holidays = holiday_events.get(
                selected_date,
                []
            )
            selected_meetings = meeting_events.get(
                selected_date,
                []
            )

        except ValueError:
            selected_date = None
            selected_tasks = []
            selected_holidays = []
            selected_meetings = []

    if month == 1:
        prev_month = 12
        prev_year = year - 1
    else:
        prev_month = month - 1
        prev_year = year

    if month == 12:
        next_month = 1
        next_year = year + 1
    else:
        next_month = month + 1
        next_year = year

    clients = Client.query.filter_by(
        status="active"
    ).order_by(
        Client.client_name.asc()
    ).all()

    employees = User.query.filter(
        User.status == "active"
    ).order_by(
        User.name.asc()
    ).all()

    statuses = task_status.ALL_STATUSES

    month_name = calendar.month_name[month]

    # ---------------------------------------------------------------
    # View mode: month (default) / week / list. Week and list reuse the
    # events already gathered above, so they add no queries - just a
    # different slice of the same data.
    # ---------------------------------------------------------------
    view = request.args.get("view", "month").strip().lower()
    if view not in ("month", "week", "list"):
        view = "month"

    # Reference date for the week view (?date=YYYY-MM-DD), default today.
    ref_date = None
    date_arg = request.args.get("date", "").strip()
    if date_arg:
        try:
            ref_date = datetime.strptime(date_arg, "%Y-%m-%d").date()
        except ValueError:
            ref_date = None
    if ref_date is None:
        ref_date = today.date()

    # The week containing ref_date, Sunday-first to match the month grid
    # (Python weekday(): Mon=0..Sun=6, so Sunday's offset is (wd + 1) % 7).
    week_start = ref_date - timedelta(days=(ref_date.weekday() + 1) % 7)
    week_days = [week_start + timedelta(days=i) for i in range(7)]
    prev_week = (week_start - timedelta(days=7)).isoformat()
    next_week = (week_start + timedelta(days=7)).isoformat()
    this_week = today.date().isoformat()
    week_range_label = (
        f"{week_days[0].strftime('%d %b')} - "
        f"{week_days[6].strftime('%d %b %Y')}"
    )

    # List view: every day with something on it, from a week back through
    # the next two months - "what's coming up", in order.
    list_start = today.date() - timedelta(days=7)
    list_end = today.date() + timedelta(days=60)
    all_event_dates = set(events) | set(holiday_events) | set(meeting_events)
    list_dates = sorted(
        d for d in all_event_dates if list_start <= d <= list_end
    )

    return render_template(
        "calendar/index.html",

        today=today.date(),

        view=view,
        week_days=week_days,
        prev_week=prev_week,
        next_week=next_week,
        this_week=this_week,
        week_range_label=week_range_label,
        list_dates=list_dates,

        month=month,
        year=year,
        month_name=month_name,

        month_days=month_days,
        events=events,
        holiday_events=holiday_events,
        meeting_events=meeting_events,

        prev_month=prev_month,
        prev_year=prev_year,

        next_month=next_month,
        next_year=next_year,

        clients=clients,
        employees=employees,
        statuses=statuses,

        selected_client=selected_client,
        selected_employee=selected_employee,
        selected_status=selected_status,

        selected_day=selected_day,
        selected_date=selected_date,
        selected_tasks=selected_tasks,
        selected_holidays=selected_holidays,
        selected_meetings=selected_meetings
    )