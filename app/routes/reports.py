from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models import DailyReport, Task, User
from app.utils.permissions import has_permission


reports_bp = Blueprint(
    "reports",
    __name__,
    url_prefix="/reports"
)


def format_report_time(seconds):
    seconds = int(seconds or 0)

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    if hours and minutes:
        return f"{hours}h {minutes}m"

    if hours:
        return f"{hours}h"

    return f"{minutes}m"


def count_lines(value):
    if not value:
        return 0

    lines = [
        line.strip()
        for line in value.splitlines()
        if line.strip()
    ]

    return len(lines)


def build_report_rows(reports):
    rows = []

    for report in reports:
        employee = report.employee
        report_date = report.report_date

        completed_tasks = Task.query.filter(
            Task.assigned_to_id == employee.id,
            Task.employee_completed == True,
            Task.employee_completed_at.isnot(None)
        ).all()

        completed_count = len([
            task for task in completed_tasks
            if task.employee_completed_at.date() == report_date
        ])

        in_progress_count = count_lines(
            report.in_progress_work
        )

        rows.append({
            "report": report,
            "employee": employee,
            "completed_count": completed_count,
            "in_progress_count": in_progress_count,
            "worked_time": report.hours_worked or 0
        })

    return rows


def parse_filter_date(value):
    if not value:
        return None

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


@reports_bp.route("/")
@login_required
def list_reports():

    can_view_all = has_permission(current_user, "view_reports")

    selected_employee = request.args.get("employee_id", "").strip()
    from_date_raw = request.args.get("from_date", "").strip()
    to_date_raw = request.args.get("to_date", "").strip()

    from_date = parse_filter_date(from_date_raw)
    to_date = parse_filter_date(to_date_raw)

    if can_view_all:
        query = DailyReport.query
    else:
        query = DailyReport.query.filter_by(employee_id=current_user.id)

    # Only a viewer who can already see every employee's reports is
    # allowed to narrow by employee - for anyone else the base query
    # above already scopes to just their own, so this filter would be
    # a no-op at best and a false sense of control at worst.
    if can_view_all and selected_employee.isdigit():
        query = query.filter(DailyReport.employee_id == int(selected_employee))

    if from_date:
        query = query.filter(DailyReport.report_date >= from_date)

    if to_date:
        query = query.filter(DailyReport.report_date <= to_date)

    page = request.args.get("page", 1, type=int)

    pagination = query.order_by(
        DailyReport.report_date.desc(),
        DailyReport.created_at.desc()
    ).paginate(
        page=page,
        per_page=25,
        error_out=False
    )

    report_rows = build_report_rows(pagination.items)

    employees = []

    if can_view_all:
        employees = User.query.filter(
            User.status == "active"
        ).order_by(
            User.name.asc()
        ).all()

    is_filtered = bool(
        (can_view_all and selected_employee) or from_date_raw or to_date_raw
    )

    return render_template(
        "reports/list.html",
        report_rows=report_rows,
        pagination=pagination,
        can_view_all=can_view_all,
        employees=employees,
        selected_employee=selected_employee,
        from_date=from_date_raw,
        to_date=to_date_raw,
        is_filtered=is_filtered
    )


@reports_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_report():

    today = date.today()

    completed_tasks = Task.query.filter(
        Task.assigned_to_id == current_user.id,
        Task.employee_completed == True,
        Task.employee_completed_at.isnot(None)
    ).all()

    completed_tasks = [
        task for task in completed_tasks
        if task.employee_completed_at.date() == today
    ]

    in_progress_tasks = Task.query.filter(
        Task.assigned_to_id == current_user.id,
        Task.status == "In Progress"
    ).all()

    assigned_tasks = Task.query.filter(
        Task.assigned_to_id == current_user.id,
        Task.status == "Assigned"
    ).all()

    total_seconds = sum(
        task.worked_seconds or 0
        for task in completed_tasks
    )

    total_hours = round(
        total_seconds / 3600,
        2
    )

    if request.method == "POST":

        report = DailyReport(
            employee_id=current_user.id,
            report_date=today,
            completed_work=request.form.get("completed_work"),
            in_progress_work=request.form.get("in_progress_work"),
            hours_worked=total_hours,
            issues=request.form.get("issues"),
            tomorrow_plan=request.form.get("tomorrow_plan")
        )

        db.session.add(report)
        db.session.commit()

        flash(
            "Daily report submitted successfully.",
            "success"
        )

        return redirect(
            url_for("reports.list_reports")
        )

    return render_template(
        "reports/add.html",
        completed_tasks=completed_tasks,
        in_progress_tasks=in_progress_tasks,
        assigned_tasks=assigned_tasks,
        total_hours=total_hours
    )


@reports_bp.route("/<int:report_id>")
@login_required
def view_report(report_id):

    report = DailyReport.query.get_or_404(report_id)

    if (
        report.employee_id != current_user.id
        and not has_permission(current_user, "view_reports")
    ):
        return redirect(
            url_for("reports.list_reports")
        )

    report_date = report.report_date
    employee = report.employee

    completed_tasks = Task.query.filter(
        Task.assigned_to_id == employee.id,
        Task.employee_completed == True,
        Task.employee_completed_at.isnot(None)
    ).all()

    completed_tasks = [
        task for task in completed_tasks
        if task.employee_completed_at.date() == report_date
    ]

    in_progress_tasks = Task.query.filter(
        Task.assigned_to_id == employee.id,
        Task.status == "In Progress"
    ).all()

    assigned_tasks = Task.query.filter(
        Task.assigned_to_id == employee.id,
        Task.status == "Assigned"
    ).all()

    total_seconds = sum(
        task.worked_seconds or 0
        for task in completed_tasks
    )

    total_worked_time = format_report_time(total_seconds)

    completed_task_rows = []

    for task in completed_tasks:
        completed_task_rows.append({
            "task": task,
            "worked_time": format_report_time(task.worked_seconds or 0),
            "submitted_at": (
                task.employee_completed_at + timedelta(hours=5, minutes=30)
                if task.employee_completed_at
                else None
            )
        })

    in_progress_task_rows = []

    for task in in_progress_tasks:
        in_progress_task_rows.append({
            "task": task,
            "worked_time": format_report_time(task.worked_seconds or 0),
            "started_at": (
                task.started_at + timedelta(hours=5, minutes=30)
                if task.started_at
                else None
            )
        })

    assigned_task_rows = []

    for task in assigned_tasks:
        assigned_task_rows.append({
            "task": task,
            "deadline": task.deadline
        })

    submitted_at = (
        report.created_at + timedelta(hours=5, minutes=30)
        if report.created_at
        else None
    )

    return render_template(
        "reports/view.html",
        report=report,
        employee=employee,
        completed_task_rows=completed_task_rows,
        in_progress_task_rows=in_progress_task_rows,
        assigned_task_rows=assigned_task_rows,
        completed_count=len(completed_task_rows),
        in_progress_count=len(in_progress_task_rows),
        assigned_count=len(assigned_task_rows),
        total_worked_time=total_worked_time,
        submitted_at=submitted_at
    )