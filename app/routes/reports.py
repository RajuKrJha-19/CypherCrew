from datetime import date, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models import DailyReport, Task
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


@reports_bp.route("/")
@login_required
def list_reports():

    if has_permission(current_user, "view_reports"):

        reports = DailyReport.query.order_by(
            DailyReport.report_date.desc(),
            DailyReport.created_at.desc()
        ).all()

    else:

        reports = DailyReport.query.filter_by(
            employee_id=current_user.id
        ).order_by(
            DailyReport.report_date.desc(),
            DailyReport.created_at.desc()
        ).all()

    report_rows = build_report_rows(reports)

    return render_template(
        "reports/list.html",
        report_rows=report_rows
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