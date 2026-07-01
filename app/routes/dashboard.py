from datetime import datetime, timedelta, date

from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required, current_user
from sqlalchemy import func

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


@dashboard_bp.route("/super-admin")
@login_required
def super_admin():

    tasks = Task.query.all()

    stats = build_task_stats(tasks)
    workload = build_workload()
    company_health = build_company_health()
    live_employees = build_live_employees()
    running_tasks = build_running_tasks()
    recent_activities = build_recent_activities()

    overview = build_overview()
    today_snapshot = build_today_snapshot()
    status_chart = build_status_chart(stats)
    month_chart = build_month_chart()
    overdue_tasks = build_overdue_tasks()
    top_employees = build_top_employees()
    top_clients = build_top_clients()
    upcoming_events = build_upcoming_events()

    return render_template(
        "dashboard/super_admin.html",
        stats=stats,
        workload=workload,
        company_health=company_health,
        live_employees=live_employees,
        running_tasks=running_tasks,
        recent_activities=recent_activities,
        overview=overview,
        today_snapshot=today_snapshot,
        status_chart=status_chart,
        month_chart=month_chart,
        overdue_tasks=overdue_tasks,
        top_employees=top_employees,
        top_clients=top_clients,
        upcoming_events=upcoming_events
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
    workload = build_workload()
    overview = build_overview()
    today_snapshot = build_today_snapshot()
    status_chart = build_status_chart(stats)
    month_chart = build_month_chart()

    return render_template(
        "dashboard/admin.html",
        user_permissions=user_permissions,
        stats=stats,
        workload=workload,
        overview=overview,
        today_snapshot=today_snapshot,
        status_chart=status_chart,
        month_chart=month_chart
    )


@dashboard_bp.route("/employee")
@login_required
def employee():

    user_permissions = [
        item.permission.name
        for item in current_user.permissions
    ]

    stats = build_task_stats(
        Task.query.filter_by(
            assigned_to_id=current_user.id
        ).all()
    )

    return render_template(
        "dashboard/employee.html",
        user_permissions=user_permissions,
        stats=stats
    )


def build_workload():

    active_statuses = [
        "Pending",
        "In Progress",
        "Core Review",
        "Client Review"
    ]

    employees = User.query.filter(
        User.role == "employee",
        User.status == "active"
    ).order_by(User.name.asc()).all()

    workload = []

    for employee in employees:
        tasks = Task.query.filter(
            Task.assigned_to_id == employee.id,
            Task.status.in_(active_statuses)
        ).all()

        remaining_hours = 0

        for task in tasks:
            quantity = task.quantity or 1
            estimated_time = task.estimated_time or 1
            remaining_hours += quantity * estimated_time

        workload.append({
            "id": employee.id,
            "name": employee.name,
            "designation": employee.designation,
            "remaining_hours": round(remaining_hours, 2)
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
        Task.deadline < datetime.utcnow(),
        Task.status != "Published"
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

    today = date.today()

    current_month_start = today.replace(day=1)

    if today.month == 1:
        last_month_start = date(today.year - 1, 12, 1)
    else:
        last_month_start = date(today.year, today.month - 1, 1)

    this_month_tasks = Task.query.filter(
        db.func.date(Task.created_at) >= current_month_start
    ).count()

    last_month_tasks = Task.query.filter(
        db.func.date(Task.created_at) >= last_month_start,
        db.func.date(Task.created_at) < current_month_start
    ).count()

    task_growth = get_growth_data(
        this_month_tasks,
        last_month_tasks
    )

    total_tasks = Task.query.count()

    completed_tasks = Task.query.filter(
        Task.employee_completed == True
    ).count()

    this_month_completed = Task.query.filter(
        Task.employee_completed == True,
        db.func.date(Task.employee_completed_at) >= current_month_start
    ).count()

    last_month_completed = Task.query.filter(
        Task.employee_completed == True,
        db.func.date(Task.employee_completed_at) >= last_month_start,
        db.func.date(Task.employee_completed_at) < current_month_start
    ).count()

    completed_growth = get_growth_data(
        this_month_completed,
        last_month_completed
    )

    pending_tasks = Task.query.filter(
        Task.status == "Pending"
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
        "pending_tasks": pending_tasks,
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



def build_today_snapshot():

    today = date.today()
    now = datetime.utcnow()

    tasks_due_today = Task.query.filter(
        db.func.date(Task.deadline) == today
    ).count()

    overdue_tasks = Task.query.filter(
        Task.deadline < now,
        Task.status != "Published"
    ).count()

    completed_today = Task.query.filter(
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


def build_month_chart():

    today = date.today()
    start_date = today - timedelta(days=29)

    labels = []
    created_values = []
    completed_values = []

    for i in range(30):

        current_date = start_date + timedelta(days=i)

        labels.append(
            current_date.strftime("%d %b")
        )

        created_count = Task.query.filter(
            db.func.date(Task.created_at) == current_date
        ).count()

        completed_count = Task.query.filter(
            db.func.date(Task.employee_completed_at) == current_date
        ).count()

        created_values.append(created_count)
        completed_values.append(completed_count)

    return {
        "labels": labels,
        "created": created_values,
        "completed": completed_values
    }


def build_overdue_tasks():

    now = datetime.utcnow()

    tasks = Task.query.filter(
        Task.deadline < now,
        Task.status != "Published"
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

    today = date.today()
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

    status_order = [
        "Pending",
        "In Progress",
        "Core Review",
        "Client Review",
        "Published"
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

    overdue = [
        task for task in tasks
        if task.deadline
        and task.deadline < datetime.utcnow()
        and task.status != "Published"
    ]

    return {
        "total": len(tasks),
        "published": status_counts.get("Published", 0),
        "employee_completed": employee_completed,
        "in_review": status_counts.get("Core Review", 0)
        + status_counts.get("Client Review", 0),
        "overdue": len(overdue),
        "status_counts": status_counts,
        "status_order": status_order,
        "recent_tasks": sorted(
            tasks,
            key=lambda item: item.created_at or datetime.min,
            reverse=True
        )[:5]
    }
