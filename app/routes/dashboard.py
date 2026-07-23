from datetime import datetime, timedelta, date
from app.utils.timezone import ist_now, IST_OFFSET
from flask import Blueprint, render_template, redirect, url_for, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy import func
from app.utils.timezone import ist_now
from app.utils.permissions import has_permission
from app.utils import task_status
from app.extensions import db
from app.models import Task,User,Client,TaskActivity,Meeting,Holiday,Leave

dashboard_bp = Blueprint(
    "dashboard",
    __name__
)


@dashboard_bp.route("/")
@login_required
def index():

    if current_user.role == "super_admin":
        return redirect(url_for("dashboard.super_admin"))

    if current_user.role == "admin":
        return redirect(url_for("dashboard.admin"))

    if current_user.role == "employee":
        return redirect(url_for("dashboard.employee"))

    return redirect(url_for("auth.logout"))


def build_team():
    """One row per employee: live state, current task and remaining load.

    The dashboard used to answer "who is doing what" three separate
    times - Live Employees, Running Tasks and Team Workload - which on
    a one-person team meant the same person rendered three times in
    601px. build_live_employees() already carries the state and the
    current task; the only thing it lacks is the remaining hours, which
    build_workload() has. Joining them here keeps the template simple.
    """
    load_by_employee = {
        row["id"]: row
        for row in build_workload()
    }

    empty_load = {
        "remaining_hours": 0,
        "active_tasks": 0,
        "overdue_tasks": 0,
        "in_review": 0,
        "days": 0,
        "level": "clear",
        "load_percent": 0,
    }

    team = []

    for employee in build_live_employees():
        row = dict(employee)
        load = load_by_employee.get(employee["employee_id"], empty_load)

        for key in empty_load:
            row[key] = load[key]

        team.append(row)

    # Whoever is actually working sorts first - that is the part of the
    # list a manager is looking for.
    state_order = {"working": 0, "paused": 1, "idle": 2}

    team.sort(
        key=lambda row: (
            state_order.get(row["state"], 3),
            row["employee_name"].lower(),
        )
    )

    return team


@dashboard_bp.route("/super-admin")
@login_required
def super_admin():

    tasks = Task.query.all()

    stats = build_task_stats(tasks)
    recent_activities = build_recent_activities()

    overview = build_overview()
    today_snapshot = build_today_snapshot()
    status_chart = build_status_chart(stats)

    # Forward-looking agenda for the next seven days - deadlines,
    # meetings, holidays and leave in one glance. Nothing else on the
    # dashboard looks ahead, so this is the slot that used to hold a
    # second, redundant created-vs-completed trend.
    upcoming = build_upcoming_events()

    # Period-scoped Performance band: throughput for a chosen window,
    # driven by the date picker on that section. Everything above stays
    # a live "right now" view.
    period = resolve_period(request.args)
    activity = build_activity(period)
    activity_trend = build_activity_trend(period)

    # A manager who also carries tasks sees their own next action first.
    # Reuses the employee helpers; the template hides the band entirely
    # when there is nothing assigned, so pure-oversight roles never see
    # an empty personal section.
    my_focus = build_my_focus(current_user)
    my_queue = build_my_queue(
        current_user,
        exclude_id=my_focus["task"].id if my_focus else None,
        limit=4,
    )

    team = build_team()

    # build_company_health(), build_overdue_tasks(), build_top_employees()
    # and build_top_clients() used to be computed here and handed to a
    # template that referenced none of them - four sets of queries run on
    # every dashboard load and thrown away. The overdue count the page
    # actually needed was already in today_snapshot.
    return render_template(
        "dashboard/super_admin.html",
        stats=stats,
        team=team,
        recent_activities=recent_activities,
        overview=overview,
        today_snapshot=today_snapshot,
        status_chart=status_chart,
        upcoming=upcoming,
        period=period,
        activity=activity,
        activity_trend=activity_trend,
        my_focus=my_focus,
        my_queue=my_queue,
    )


@dashboard_bp.route("/admin")
@login_required
def admin():

    user_permissions = [
        item.permission.name
        for item in current_user.permissions
    ]

    tasks = Task.query.all()

    stats = build_task_stats(tasks)
    overview = build_overview()
    today_snapshot = build_today_snapshot()
    status_chart = build_status_chart(stats)
    upcoming = build_upcoming_events()

    team = build_team()

    # The admin dashboard had no activity feed, so an admin could not
    # see what the team had just done without opening tasks one by one.
    recent_activities = build_recent_activities()

    period = resolve_period(request.args)
    activity = build_activity(period)
    activity_trend = build_activity_trend(period)

    my_focus = build_my_focus(current_user)
    my_queue = build_my_queue(
        current_user,
        exclude_id=my_focus["task"].id if my_focus else None,
        limit=4,
    )

    return render_template(
        "dashboard/admin.html",
        user_permissions=user_permissions,
        stats=stats,
        team=team,
        recent_activities=recent_activities,
        overview=overview,
        today_snapshot=today_snapshot,
        status_chart=status_chart,
        upcoming=upcoming,
        period=period,
        activity=activity,
        activity_trend=activity_trend,
        my_focus=my_focus,
        my_queue=my_queue,
    )

@dashboard_bp.route("/api/overview")
@login_required
def api_overview():

    # Deliberately the cheap half of the dashboard only - build_overview()
    # is a handful of plain .count() queries, unlike build_workload(),
    # build_company_health() or build_top_employees()/build_top_clients(),
    # which loop and issue one query per employee/client. Polling those
    # every few seconds is exactly the slowdown a live-refresh feature
    # should not introduce, so only this cheap overview is exposed here.
    if current_user.role not in ["admin", "super_admin"]:
        return jsonify(success=False), 403

    overview = build_overview()

    return jsonify(
        success=True,
        total_tasks=overview["total_tasks"],
        completed_tasks=overview["completed_tasks"],
        pending_tasks=overview["pending_tasks"],
        active_clients=overview["active_clients"],
        active_employees=overview["active_employees"],
        meetings_today=overview["meetings_today"],
    )


@dashboard_bp.route("/api/my-stats")
@login_required
def api_my_stats():

    # The same counts the employee dashboard renders with, as direct
    # COUNT queries - the cheap shape the tiles poll every few seconds.
    # Sharing build_my_counts() keeps the polled numbers and the
    # server-rendered ones from ever drifting apart.
    return jsonify(success=True, **build_my_counts(current_user))


@dashboard_bp.route("/my-tasks")
@login_required
def my_tasks():

    if current_user.role not in ["admin", "super_admin"] and not has_permission(current_user, "approve_tasks"):
        return redirect(url_for("dashboard.index"))

    core_review_tasks = Task.query.filter(
        Task.status == "Core Review"
    ).order_by(
        Task.employee_completed_at.desc()
    ).all()

    client_review_tasks = Task.query.filter(
        Task.status == "Client Review"
    ).order_by(
        Task.id.desc()
    ).all()

    published_tasks = Task.query.filter(
        Task.status == "Published"
    ).order_by(
        Task.completed_at.desc()
    ).limit(30).all()

    return render_template(
        "dashboard/my_tasks.html",
        core_review_tasks=core_review_tasks,
        client_review_tasks=client_review_tasks,
        published_tasks=published_tasks
    )

@dashboard_bp.route("/employee")
@login_required
def employee():

    user_permissions = [
        item.permission.name
        for item in current_user.permissions
    ]

    # Built around the one question an employee opens this page to
    # answer - "what should I be doing right now" - rather than a wall
    # of counts. Focus is the task to pick up, queue is what's next,
    # deadlines and meetings are the pressure to plan around.
    counts = build_my_counts(current_user)
    focus = build_my_focus(current_user)
    queue = build_my_queue(
        current_user,
        exclude_id=focus["task"].id if focus else None
    )
    deadlines = build_my_deadlines(current_user)
    meetings_today = build_my_meetings_today(current_user)

    return render_template(
        "dashboard/employee.html",
        user_permissions=user_permissions,
        counts=counts,
        focus=focus,
        queue=queue,
        deadlines=deadlines,
        meetings_today=meetings_today,
    )


#: A working day, used to turn an hours figure into something a person
#: can actually reason about. "12 hours" means little on its own;
#: "about a day and a half" means something.
WORKING_DAY_HOURS = 8

#: A full week is treated as a loaded plate - the point at which the
#: bar is full and the label reads "overloaded".
WORKLOAD_FULL_HOURS = WORKING_DAY_HOURS * 5


def build_workload():
    """Remaining work per employee, in terms they can act on.

    The old figure was the sum of every estimate on an employee's
    plate, which was misleading twice over.

    It counted the whole estimate for a task that was nearly finished,
    because it never looked at worked_seconds - so a task estimated at
    8 hours with 7 already logged still reported 8 hours of load.

    And it counted Core Review and Client Review, which are somebody
    else's queue: once work is submitted the assignee cannot move it,
    so it is not load they can burn down. On Hold was already excluded
    for exactly that reason; review was left in by oversight.

    So "8.0 hrs" could have meant eight hours of real work, or one
    task that was 95% done, or one sitting with the client for a week.

    This returns what is genuinely left to do, plus the context needed
    to read it: how many tasks, how many are overdue, roughly how many
    days it represents, and how heavy that is.
    """

    # What the assignee can still act on. Paused counts - they paused
    # it and can resume it. On Hold and the review states do not:
    # those are waiting on somebody else.
    actionable_statuses = [
        task_status.ASSIGNED,
        task_status.IN_PROGRESS,
        task_status.PAUSED,
    ]

    review_statuses = [
        task_status.CORE_REVIEW,
        task_status.CLIENT_REVIEW,
    ]

    employees = User.query.filter(
        User.role == "employee",
        User.status == "active"
    ).order_by(User.name.asc()).all()

    now = datetime.utcnow()
    workload = []

    for employee in employees:

        tasks = Task.query.filter(
            Task.assigned_to_id == employee.id,
            Task.status.in_(actionable_statuses)
        ).all()

        remaining_hours = 0.0
        overdue_tasks = 0

        for task in tasks:
            estimated = (task.quantity or 1) * (task.estimated_time or 1)
            worked = (task.worked_seconds or 0) / 3600

            # An overrun is not negative work left; it is zero.
            remaining_hours += max(0.0, estimated - worked)

            if task.deadline and task.deadline < now:
                overdue_tasks += 1

        # Their work that is out of their hands, shown separately so
        # the main figure stays "what you can pick up right now".
        in_review = Task.query.filter(
            Task.assigned_to_id == employee.id,
            Task.status.in_(review_statuses)
        ).count()

        days = remaining_hours / WORKING_DAY_HOURS

        if remaining_hours <= 0:
            level = "clear"
        elif days <= 1:
            level = "light"
        elif days <= 3:
            level = "steady"
        elif days <= 5:
            level = "heavy"
        else:
            level = "over"

        workload.append({
            "id": employee.id,
            "name": employee.name,
            "designation": employee.designation,
            "remaining_hours": round(remaining_hours, 1),
            "active_tasks": len(tasks),
            "overdue_tasks": overdue_tasks,
            "in_review": in_review,
            "days": round(days, 1),
            "level": level,
            # Percentage of a full week, for the bar. Capped so an
            # extreme outlier cannot overflow its track.
            "load_percent": min(
                100,
                round(remaining_hours / WORKLOAD_FULL_HOURS * 100),
            ),
        })

    workload.sort(
        key=lambda item: item["remaining_hours"],
        reverse=True
    )

    return workload
def build_company_health():

    employees = User.query.filter(
        User.role == "employee",
        User.status == "active"
    ).all()

    working = 0
    paused = 0
    idle = 0

    for employee in employees:

        running_task = Task.query.filter(
            Task.assigned_to_id == employee.id,
            Task.status == "In Progress",
            Task.timer_started_at.isnot(None)
        ).first()

        if running_task:
            working += 1
            continue

        paused_task = Task.query.filter(
            Task.assigned_to_id == employee.id,
            Task.status == "In Progress",
            Task.timer_started_at.is_(None)
        ).first()

        if paused_task:
            paused += 1
        else:
            idle += 1

    overdue = Task.query.filter(
        Task.deadline < ist_now(),
        Task.status.in_(task_status.OVERDUE_STATUSES)
    ).count()

    return {
        "working": working,
        "paused": paused,
        "idle": idle,
        "overdue": overdue
    }
def seconds_to_hms(seconds):

    seconds = int(seconds or 0)

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    remaining_seconds = seconds % 60

    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


def get_task_live_seconds(task):

    total_seconds = task.worked_seconds or 0

    if task.timer_started_at:
        total_seconds += int(
            (datetime.utcnow() - task.timer_started_at).total_seconds()
        )

    return total_seconds


def get_task_estimated_seconds(task):

    quantity = task.quantity or 1
    estimated_time = task.estimated_time or 1

    return int(quantity * estimated_time * 3600)


def build_live_employees():

    employees = User.query.filter(
        User.role == "employee",
        User.status == "active"
    ).order_by(
        User.name.asc()
    ).all()

    live_employees = []

    for employee in employees:

        running_task = Task.query.filter(
            Task.assigned_to_id == employee.id,
            Task.status == "In Progress",
            Task.timer_started_at.isnot(None)
        ).order_by(
            Task.timer_started_at.desc()
        ).first()

        if running_task:

            live_seconds = get_task_live_seconds(running_task)
            estimated_seconds = get_task_estimated_seconds(running_task)
            remaining_seconds = estimated_seconds - live_seconds

            live_employees.append({
                "employee_id": employee.id,
                "employee_name": employee.name,
                "designation": employee.designation,
                "state": "working",
                "task_id": running_task.id,
                "task_title": running_task.title,
                "client_name": running_task.client.client_name if running_task.client else "-",
                "live_seconds": live_seconds,
                "live_time": seconds_to_hms(live_seconds),
                "estimated_seconds": estimated_seconds,
                "estimated_time": seconds_to_hms(estimated_seconds),
                "remaining_seconds": remaining_seconds,
                "remaining_time": seconds_to_hms(max(remaining_seconds, 0)),
                "over_estimate": remaining_seconds < 0
            })

            continue

        paused_task = Task.query.filter(
            Task.assigned_to_id == employee.id,
            Task.status == "In Progress",
            Task.timer_started_at.is_(None)
        ).order_by(
            Task.id.desc()
        ).first()

        if paused_task:

            live_seconds = get_task_live_seconds(paused_task)
            estimated_seconds = get_task_estimated_seconds(paused_task)
            remaining_seconds = estimated_seconds - live_seconds

            live_employees.append({
                "employee_id": employee.id,
                "employee_name": employee.name,
                "designation": employee.designation,
                "state": "paused",
                "task_id": paused_task.id,
                "task_title": paused_task.title,
                "client_name": paused_task.client.client_name if paused_task.client else "-",
                "live_seconds": live_seconds,
                "live_time": seconds_to_hms(live_seconds),
                "estimated_seconds": estimated_seconds,
                "estimated_time": seconds_to_hms(estimated_seconds),
                "remaining_seconds": remaining_seconds,
                "remaining_time": seconds_to_hms(max(remaining_seconds, 0)),
                "over_estimate": remaining_seconds < 0
            })

            continue

        live_employees.append({
            "employee_id": employee.id,
            "employee_name": employee.name,
            "designation": employee.designation,
            "state": "idle",
            "task_id": None,
            "task_title": "No active task",
            "client_name": "-",
            "live_seconds": 0,
            "live_time": "00:00:00",
            "estimated_seconds": 0,
            "estimated_time": "00:00:00",
            "remaining_seconds": 0,
            "remaining_time": "00:00:00",
            "over_estimate": False
        })

    return live_employees


def build_running_tasks():

    tasks = Task.query.filter(
        Task.status == "In Progress",
        Task.timer_started_at.isnot(None)
    ).order_by(
        Task.timer_started_at.desc()
    ).all()

    running_tasks = []

    for task in tasks:

        live_seconds = get_task_live_seconds(task)
        estimated_seconds = get_task_estimated_seconds(task)
        remaining_seconds = estimated_seconds - live_seconds

        running_tasks.append({
            "task_id": task.id,
            "task_title": task.title,
            "employee_name": task.assigned_to.name if task.assigned_to else "-",
            "client_name": task.client.client_name if task.client else "-",
            "live_seconds": live_seconds,
            "live_time": seconds_to_hms(live_seconds),
            "estimated_seconds": estimated_seconds,
            "estimated_time": seconds_to_hms(estimated_seconds),
            "remaining_seconds": remaining_seconds,
            "remaining_time": seconds_to_hms(max(remaining_seconds, 0)),
            "over_estimate": remaining_seconds < 0
        })

    return running_tasks


def build_recent_activities():

    activities = TaskActivity.query.order_by(
        TaskActivity.created_at.desc()
    ).limit(10).all()

    return activities

def get_growth_data(current_value, previous_value):

    if previous_value == 0 and current_value == 0:
        return {
            "text": "No change",
            "class": "neutral",
            "arrow": "–"
        }

    if previous_value == 0 and current_value > 0:
        return {
            "text": f"{current_value} new this month",
            "class": "up",
            "arrow": "↑"
        }

    difference = current_value - previous_value
    percentage = round((difference / previous_value) * 100)

    if difference > 0:
        return {
            "text": f"{percentage}% vs last month",
            "class": "up",
            "arrow": "↑"
        }

    if difference < 0:
        return {
            "text": f"{abs(percentage)}% vs last month",
            "class": "down",
            "arrow": "↓"
        }

    return {
        "text": "No change",
        "class": "neutral",
        "arrow": "–"
    }

def build_overview():

    today = ist_now().date()

    current_month_start = today.replace(day=1)

    if today.month == 1:
        last_month_start = date(today.year - 1, 12, 1)
    else:
        last_month_start = date(today.year, today.month - 1, 1)

    # Voided tasks were cancelled by the client. Counting them in the
    # denominator would drag the completion rate down for work the
    # team never got to finish, so they are excluded outright - which
    # includes the month-on-month figures, otherwise the headline and
    # its own sub-label would disagree.
    not_void = Task.status.notin_(task_status.EXCLUDED_FROM_METRICS)

    this_month_tasks = Task.query.filter(
        not_void,
        db.func.date(Task.created_at) >= current_month_start
    ).count()

    last_month_tasks = Task.query.filter(
        not_void,
        db.func.date(Task.created_at) >= last_month_start,
        db.func.date(Task.created_at) < current_month_start
    ).count()

    task_growth = get_growth_data(
        this_month_tasks,
        last_month_tasks
    )

    total_tasks = Task.query.filter(not_void).count()

    completed_tasks = Task.query.filter(
        not_void,
        Task.employee_completed == True
    ).count()

    this_month_completed = Task.query.filter(
        not_void,
        Task.employee_completed == True,
        db.func.date(Task.employee_completed_at) >= current_month_start
    ).count()

    last_month_completed = Task.query.filter(
        not_void,
        Task.employee_completed == True,
        db.func.date(Task.employee_completed_at) >= last_month_start,
        db.func.date(Task.employee_completed_at) < current_month_start
    ).count()

    completed_growth = get_growth_data(
        this_month_completed,
        last_month_completed
    )

    assigned_tasks = Task.query.filter(
        Task.status == "Assigned"
    ).count()

    active_clients = Client.query.filter_by(
        status="active"
    ).count()

    active_employees = User.query.filter(
        User.status == "active",
        User.role == "employee"
    ).count()

    meetings_today = Meeting.query.filter(
        db.func.date(Meeting.meeting_date) == today
    ).count()

    return {
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
        "pending_tasks": assigned_tasks,
        "active_clients": active_clients,
        "active_employees": active_employees,
        "meetings_today": meetings_today,

        "task_growth": task_growth,
        "completed_growth": completed_growth,
        "pending_note": {
            "text": "Current pending",
            "class": "neutral",
            "arrow": "–"
        },
        "client_note": {
            "text": "Active clients",
            "class": "neutral",
            "arrow": "–"
        },
        "employee_note": {
            "text": "Active employees",
            "class": "neutral",
            "arrow": "–"
        },
        "meeting_note": {
            "text": "Scheduled today",
            "class": "neutral",
            "arrow": "–"
        }
    }



#: Presets offered by the Performance section's date picker, in the
#: order they render. "custom" is handled separately (it carries its
#: own from/to), so it is not listed here.
PERIOD_PRESETS = ["today", "yesterday", "7d", "30d"]

#: A custom range longer than this is clamped from the start end, so a
#: careless "from 2015" cannot turn the per-day throughput loop into
#: hundreds of COUNT queries. Three months of daily bars is already the
#: point past which the chart switches to a line anyway.
MAX_PERIOD_DAYS = 92


def resolve_period(args):
    """Turn the request's query string into a concrete date window.

    The Performance band answers "what did the team actually produce in
    this window" - created, completed, published - so it needs a start
    and an end, a human label for the header, and the matching window
    immediately before it to draw the up/down deltas against.

    Everything else on the dashboard (live team state, the status
    doughnut, the In Progress / Overdue / Due Today counters) is a
    snapshot of *now* and is deliberately left untouched by this.
    """

    today = ist_now().date()
    key = (args.get("period") or "7d").lower()

    if key == "today":
        start = end = today
        label = "Today"

    elif key == "yesterday":
        start = end = today - timedelta(days=1)
        label = "Yesterday"

    elif key == "30d":
        start, end = today - timedelta(days=29), today
        label = "Last 30 days"

    elif key == "custom":
        start = _parse_date(args.get("from")) or today - timedelta(days=6)
        end = _parse_date(args.get("to")) or today

        # A backwards range is a slip, not an intent - read it the way
        # the user clearly meant it rather than showing nothing.
        if start > end:
            start, end = end, start

        # Keep the per-day loop bounded regardless of what was typed.
        if (end - start).days > MAX_PERIOD_DAYS - 1:
            start = end - timedelta(days=MAX_PERIOD_DAYS - 1)

        label = _format_range_label(start, end)
        key = "custom"

    else:
        # Unknown or default: last 7 days.
        key = "7d"
        start, end = today - timedelta(days=6), today
        label = "Last 7 days"

    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)

    return {
        "key": key,
        "label": label,
        "start": start,
        "end": end,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "prev_start": prev_start,
        "prev_end": prev_end,
        "span_days": span,
        "today": today.isoformat(),
    }


def _parse_date(value):

    if not value:
        return None

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _format_range_label(start, end):

    if start == end:
        return start.strftime("%d %b %Y")

    if start.year == end.year:
        return f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"

    return f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"


def _period_delta(current, previous):
    """A vs-previous-period badge, worded for an arbitrary window.

    get_growth_data() says "this month" in its copy, which is wrong for
    a Today or a custom range, so the Performance band computes its own.
    """

    if previous == 0 and current == 0:
        return {"text": "No change", "class": "neutral", "arrow": "–"}

    if previous == 0:
        return {"text": "vs 0 before", "class": "up", "arrow": "↑"}

    difference = current - previous
    percentage = round(abs(difference) / previous * 100)

    if difference > 0:
        return {"text": f"{percentage}% vs prev", "class": "up", "arrow": "↑"}

    if difference < 0:
        return {"text": f"{percentage}% vs prev", "class": "down", "arrow": "↓"}

    return {"text": "No change", "class": "neutral", "arrow": "–"}


def _format_turnaround(seconds):

    if not seconds or seconds <= 0:
        return "–"

    hours = seconds / 3600

    if hours < 1:
        return f"{round(seconds / 60)}m"

    if hours < 24:
        return f"{round(hours, 1)}h"

    return f"{round(hours / 24, 1)}d"


def _count_created(start, end):

    # created_at is stored in UTC (default=datetime.utcnow), which is
    # the same convention build_overview() compares against - kept
    # consistent here rather than corrected in one place only.
    return Task.query.filter(
        Task.status.notin_(task_status.EXCLUDED_FROM_METRICS),
        db.func.date(Task.created_at) >= start,
        db.func.date(Task.created_at) <= end,
    ).count()


def _count_completed(start, end):

    # employee_completed_at is stamped with ist_now(), so comparing it
    # to IST dates is exact.
    return Task.query.filter(
        Task.status.notin_(task_status.EXCLUDED_FROM_METRICS),
        Task.employee_completed == True,
        db.func.date(Task.employee_completed_at) >= start,
        db.func.date(Task.employee_completed_at) <= end,
    ).count()


def _count_published(start, end):

    # completed_at is stamped at publish time, also with ist_now().
    return Task.query.filter(
        Task.status == task_status.PUBLISHED,
        db.func.date(Task.completed_at) >= start,
        db.func.date(Task.completed_at) <= end,
    ).count()


def build_activity(period):
    """Throughput for the selected window, each figure paired with the
    same-length window before it so the tiles can show a trend."""

    start, end = period["start"], period["end"]
    prev_start, prev_end = period["prev_start"], period["prev_end"]

    created = _count_created(start, end)
    completed = _count_completed(start, end)
    published = _count_published(start, end)

    # Average time from a task being created to it being published, over
    # the tasks published in this window. created_at is UTC and
    # completed_at is IST (+5:30), so the raw subtraction runs 5h30 long
    # - back that offset out, then floor at zero so a clock-skew edge
    # case can never read as negative turnaround.
    published_tasks = Task.query.filter(
        Task.status == task_status.PUBLISHED,
        db.func.date(Task.completed_at) >= start,
        db.func.date(Task.completed_at) <= end,
        Task.created_at.isnot(None),
        Task.completed_at.isnot(None),
    ).all()

    turnaround_seconds = 0
    if published_tasks:
        total = 0
        for task in published_tasks:
            delta = (task.completed_at - task.created_at) - IST_OFFSET
            total += max(0, delta.total_seconds())
        turnaround_seconds = total / len(published_tasks)

    return {
        "created": created,
        "completed": completed,
        "published": published,
        "created_delta": _period_delta(created, _count_created(prev_start, prev_end)),
        "completed_delta": _period_delta(completed, _count_completed(prev_start, prev_end)),
        "published_delta": _period_delta(published, _count_published(prev_start, prev_end)),
        "turnaround": _format_turnaround(turnaround_seconds),
    }


def build_activity_trend(period):
    """Per-day Created / Completed / Published across the window, for the
    Performance chart. One bucket per day; a single-day window is one
    bucket, which the chart draws as a bar group rather than a lone
    point on a line."""

    start, end = period["start"], period["end"]
    span = period["span_days"]

    labels = []
    created = []
    completed = []
    published = []

    for offset in range(span):
        day = start + timedelta(days=offset)

        labels.append(day.strftime("%d %b"))
        created.append(_count_created(day, day))
        completed.append(_count_completed(day, day))
        published.append(_count_published(day, day))

    return {
        "labels": labels,
        "created": created,
        "completed": completed,
        "published": published,
    }


def build_today_snapshot():

    today = ist_now().date()
    now = ist_now()

    not_void = Task.status.notin_(task_status.EXCLUDED_FROM_METRICS)

    tasks_due_today = Task.query.filter(
        not_void,
        db.func.date(Task.deadline) == today
    ).count()

    # On Hold is left out: the delay is on the client's side, so the
    # assignee should not be shown as running late for it.
    overdue_tasks = Task.query.filter(
        Task.deadline < now,
        Task.status.in_([
            task_status.ASSIGNED,
            task_status.IN_PROGRESS,
            task_status.PAUSED
        ])
    ).count()

    completed_today = Task.query.filter(
        not_void,
        db.func.date(Task.employee_completed_at) == today
    ).count()

    meetings_today = Meeting.query.filter(
        db.func.date(Meeting.meeting_date) == today
    ).count()

    holidays_today = Holiday.query.filter(
        Holiday.holiday_date == today
    ).count()

    employees_on_leave = Leave.query.filter(
        Leave.start_date <= today,
        Leave.end_date >= today
    ).count()

    return {
        "working_employees": build_company_health()["working"],
        "employees_on_leave": employees_on_leave,
        "meetings_today": meetings_today,
        "tasks_due_today": tasks_due_today,
        "overdue_tasks": overdue_tasks,
        "completed_today": completed_today,
        "holidays_today": holidays_today
    }


def build_status_chart(stats):

    labels = stats["status_order"]

    values = [
        stats["status_counts"].get(status, 0)
        for status in labels
    ]

    return {
        "labels": labels,
        "values": values
    }


def build_overdue_tasks():

    now = ist_now()

    tasks = Task.query.filter(
        Task.deadline < now,
        Task.status.in_(task_status.OVERDUE_STATUSES)
    ).order_by(
        Task.deadline.asc()
    ).limit(5).all()

    overdue_list = []

    for task in tasks:

        days_overdue = (now.date() - task.deadline.date()).days

        overdue_list.append({
            "id": task.id,
            "title": task.title,
            "client": task.client.client_name if task.client else "-",
            "status": task.status,
            "days_overdue": days_overdue
        })

    return overdue_list


def build_top_employees():

    employees = User.query.filter(
        User.role == "employee",
        User.status == "active"
    ).order_by(
        User.name.asc()
    ).all()

    result = []

    for employee in employees:

        total = Task.query.filter(
            Task.assigned_to_id == employee.id
        ).count()

        completed = Task.query.filter(
            Task.assigned_to_id == employee.id,
            Task.employee_completed == True
        ).count()

        percentage = 0

        if total > 0:
            percentage = round((completed / total) * 100)

        result.append({
            "name": employee.name,
            "percentage": percentage
        })

    result.sort(
        key=lambda item: item["percentage"],
        reverse=True
    )

    return result[:5]


def build_top_clients():

    clients = Client.query.filter_by(
        status="active"
    ).order_by(
        Client.client_name.asc()
    ).all()

    result = []

    for client in clients:

        task_count = Task.query.filter(
            Task.client_id == client.id
        ).count()

        result.append({
            "name": client.client_name,
            "task_count": task_count
        })

    result.sort(
        key=lambda item: item["task_count"],
        reverse=True
    )

    return result[:5]


def build_upcoming_events():

    today = ist_now().date()
    next_week = today + timedelta(days=7)

    events = []

    tasks = Task.query.filter(
        Task.deadline.isnot(None),
        db.func.date(Task.deadline) >= today,
        db.func.date(Task.deadline) <= next_week
    ).order_by(
        Task.deadline.asc()
    ).limit(5).all()

    for task in tasks:
        events.append({
            "type": "Task Deadline",
            "title": task.title,
            "subtitle": task.client.client_name if task.client else "-",
            "date": task.deadline.strftime("%d %b, %I:%M %p")
        })

    meetings = Meeting.query.filter(
        db.func.date(Meeting.meeting_date) >= today,
        db.func.date(Meeting.meeting_date) <= next_week
    ).order_by(
        Meeting.meeting_date.asc()
    ).limit(5).all()

    for meeting in meetings:
        events.append({
            "type": "Meeting",
            "title": meeting.title,
            "subtitle": meeting.client.client_name if meeting.client else "Internal",
            "date": meeting.meeting_date.strftime("%d %b, %I:%M %p")
        })

    holidays = Holiday.query.filter(
        Holiday.holiday_date >= today,
        Holiday.holiday_date <= next_week
    ).order_by(
        Holiday.holiday_date.asc()
    ).limit(5).all()

    for holiday in holidays:
        events.append({
            "type": "Holiday",
            "title": holiday.title,
            "subtitle": "Company Holiday",
            "date": holiday.holiday_date.strftime("%d %b")
        })

    leaves = Leave.query.filter(
        Leave.start_date >= today,
        Leave.start_date <= next_week
    ).order_by(
        Leave.start_date.asc()
    ).limit(5).all()

    for leave in leaves:
        events.append({
            "type": "Leave",
            "title": leave.user.name if leave.user else "Employee Leave",
            "subtitle": leave.reason or "Leave",
            "date": leave.start_date.strftime("%d %b")
        })

    return events[:5]

def build_task_stats(tasks):

    status_order = task_status.BOARD_STATUSES

    # Voided work is cancelled, not delivered and not outstanding -
    # it is kept out of the chart and every figure below.
    tasks = [
        task for task in tasks
        if task.status not in task_status.EXCLUDED_FROM_METRICS
    ]

    status_counts = {
        status: 0
        for status in status_order
    }
    employee_completed = 0

    for task in tasks:

        status_counts[task.status] = (
            status_counts.get(task.status, 0) + 1
        )

        if task.employee_completed:
            employee_completed += 1

    # On Hold is blocked externally, so the assignee is not late.
    overdue = [
        task for task in tasks
        if task.deadline
        and task.deadline < ist_now()
        and task.status in [
            task_status.ASSIGNED,
            task_status.IN_PROGRESS,
            task_status.PAUSED
        ]
    ]

    return {
        "total": len(tasks),
        "published": status_counts.get(task_status.PUBLISHED, 0),
        "employee_completed": employee_completed,
        "in_review": status_counts.get(task_status.CORE_REVIEW, 0)
        + status_counts.get(task_status.CLIENT_REVIEW, 0),
        "overdue": len(overdue),
        "status_counts": status_counts,
        "status_order": status_order,
        "recent_tasks": sorted(
            tasks,
            key=lambda item: item.created_at or datetime.min,
            reverse=True
        )[:5]
    }


# ============================================================
# Employee dashboard
#
# The management dashboards answer "how is the team doing". The
# employee one answers a narrower, more useful question - "what should
# I be doing right now" - so it is built from the assignee's own work:
# the task to pick up, the queue behind it, and the deadlines and
# meetings to plan around.
# ============================================================

#: Lower rank sorts first. An unknown priority lands in the middle
#: rather than at either extreme.
PRIORITY_RANK = {"Urgent": 0, "High": 1, "Medium": 2, "Low": 3}

#: Statuses the assignee can personally move forward - what belongs in
#: their queue. Review and On Hold are waiting on someone else.
MY_ACTIONABLE = [
    task_status.ASSIGNED,
    task_status.IN_PROGRESS,
    task_status.PAUSED,
]


def build_my_counts(user):
    """The headline counts for one employee, as direct COUNTs so the
    dashboard and its live poller share exactly one source of truth."""

    now = ist_now()
    today = now.date()

    not_void = Task.status.notin_(task_status.EXCLUDED_FROM_METRICS)
    base = Task.query.filter(Task.assigned_to_id == user.id, not_void)

    return {
        "total": base.count(),
        "in_progress": base.filter(
            Task.status == task_status.IN_PROGRESS
        ).count(),
        "in_review": base.filter(
            Task.status.in_([
                task_status.CORE_REVIEW,
                task_status.CLIENT_REVIEW,
            ])
        ).count(),
        "published": base.filter(
            Task.status == task_status.PUBLISHED
        ).count(),
        "due_today": base.filter(
            db.func.date(Task.deadline) == today
        ).count(),
        "overdue": base.filter(
            Task.deadline.isnot(None),
            Task.deadline < now,
            Task.status.in_([
                task_status.ASSIGNED,
                task_status.IN_PROGRESS,
                task_status.PAUSED,
            ])
        ).count(),
        "completed_today": base.filter(
            Task.employee_completed == True,
            db.func.date(Task.employee_completed_at) == today
        ).count(),
    }


def _deadline_label(deadline, today):
    """A short, human due-date chip - "3d overdue", "Today", "in 4d"."""

    if deadline is None:
        return None

    days = (deadline.date() - today).days

    if days < 0:
        magnitude = abs(days)
        text = "1d overdue" if magnitude == 1 else f"{magnitude}d overdue"
        return {"text": text, "class": "overdue"}

    if days == 0:
        return {"text": "Today", "class": "today"}

    if days == 1:
        return {"text": "Tomorrow", "class": "soon"}

    if days <= 7:
        return {"text": f"in {days}d", "class": "soon"}

    return {"text": deadline.strftime("%d %b"), "class": "far"}


def _task_progress(task):
    """Elapsed vs estimate for a task, in the shape the focus card and
    queue rows render."""

    live = get_task_live_seconds(task)
    estimated = get_task_estimated_seconds(task)
    remaining = estimated - live

    return {
        "live_seconds": live,
        "live_time": seconds_to_hms(live),
        "estimated_time": seconds_to_hms(estimated),
        "remaining_time": seconds_to_hms(max(remaining, 0)),
        "over_estimate": remaining < 0,
        "progress": min(100, round(live / estimated * 100)) if estimated else 0,
    }


def _queue_sort_key(task, now):
    return (
        0 if (task.deadline and task.deadline < now) else 1,  # overdue first
        PRIORITY_RANK.get(task.priority, 2),                  # then priority
        task.deadline or datetime.max,                        # then soonest
        task.id,
    )


def _my_actionable_tasks(user):
    return Task.query.filter(
        Task.assigned_to_id == user.id,
        Task.status.in_(MY_ACTIONABLE),
    ).all()


def build_my_focus(user):
    """The single task to act on now: whatever timer is running, else
    the most recent paused task, else the top of the queue. Returns
    None only when the employee has nothing actionable at all."""

    now = ist_now()
    today = now.date()

    state = None

    # 1. Actually running - a live timer beats everything.
    task = Task.query.filter(
        Task.assigned_to_id == user.id,
        Task.status == task_status.IN_PROGRESS,
        Task.timer_started_at.isnot(None),
    ).order_by(Task.timer_started_at.desc()).first()

    if task:
        state = "working"

    # 2. Paused - explicitly Paused, or In Progress with the timer
    #    stopped, which is the same thing from the employee's side.
    if not task:
        task = Task.query.filter(
            Task.assigned_to_id == user.id,
            Task.status == task_status.PAUSED,
        ).order_by(Task.id.desc()).first()

        if not task:
            task = Task.query.filter(
                Task.assigned_to_id == user.id,
                Task.status == task_status.IN_PROGRESS,
                Task.timer_started_at.is_(None),
            ).order_by(Task.id.desc()).first()

        if task:
            state = "paused"

    # 3. Nothing in flight - surface the highest-priority thing to start.
    if not task:
        assigned = [
            item for item in _my_actionable_tasks(user)
            if item.status == task_status.ASSIGNED
        ]
        assigned.sort(key=lambda item: _queue_sort_key(item, now))
        task = assigned[0] if assigned else None

        if task:
            state = "next"

    if not task:
        return None

    focus = {
        "task": task,
        "state": state,
        "client": task.client.client_name if task.client else None,
        "overdue": bool(task.deadline and task.deadline < now),
        "deadline": _deadline_label(task.deadline, today),
    }
    focus.update(_task_progress(task))
    return focus


def build_my_queue(user, exclude_id=None, limit=6):
    """What to work on next: the assignee's actionable tasks, ordered
    the way they should be picked up - overdue first, then priority,
    then soonest deadline. The focus task is left out so the hero and
    the queue do not show the same row twice."""

    now = ist_now()
    today = now.date()

    tasks = _my_actionable_tasks(user)
    tasks.sort(key=lambda item: _queue_sort_key(item, now))

    rows = []

    for task in tasks:
        if exclude_id and task.id == exclude_id:
            continue

        running = (
            task.status == task_status.IN_PROGRESS
            and task.timer_started_at is not None
        )

        rows.append({
            "task": task,
            "priority": task.priority,
            "status": task.status,
            "client": task.client.client_name if task.client else "-",
            "deadline": _deadline_label(task.deadline, today),
            "running": running,
            # Anything not already running can be started or resumed
            # straight from the dashboard.
            "can_start": not running,
        })

        if len(rows) >= limit:
            break

    return rows


def build_my_deadlines(user, limit=6):
    """The assignee's own tasks coming due inside a week, overdue ones
    included, soonest first - the pressure to plan the day around."""

    now = ist_now()
    today = now.date()
    horizon = now + timedelta(days=7)

    tasks = Task.query.filter(
        Task.assigned_to_id == user.id,
        Task.deadline.isnot(None),
        Task.deadline <= horizon,
        Task.status.in_([
            task_status.ASSIGNED,
            task_status.IN_PROGRESS,
            task_status.PAUSED,
            task_status.CORE_REVIEW,
            task_status.CLIENT_REVIEW,
        ]),
    ).order_by(Task.deadline.asc()).limit(limit).all()

    return [
        {
            "task": task,
            "client": task.client.client_name if task.client else "-",
            "status": task.status,
            "deadline": _deadline_label(task.deadline, today),
        }
        for task in tasks
    ]


def build_my_meetings_today(user):
    """Meetings the employee is a participant in today - the only part
    of their day that runs on someone else's clock."""

    today = ist_now().date()

    meetings = Meeting.query.filter(
        db.func.date(Meeting.meeting_date) == today,
        Meeting.participants.any(User.id == user.id),
    ).order_by(Meeting.meeting_date.asc()).all()

    return [
        {
            "title": meeting.title,
            "time": meeting.meeting_date.strftime("%I:%M %p"),
            "client": meeting.client.client_name if meeting.client else "Internal",
        }
        for meeting in meetings
    ]
