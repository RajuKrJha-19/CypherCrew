from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required, current_user
from sqlalchemy import func

from app.extensions import db
from app.models import Task, User


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

    stats = build_task_stats(Task.query.all())
    workload = build_workload()

    return render_template(
        "dashboard/super_admin.html",
        stats=stats,
        workload=workload
    )


@dashboard_bp.route("/admin")
@login_required
def admin():

    user_permissions = [
        item.permission.name
        for item in current_user.permissions
    ]

    stats = build_task_stats(Task.query.all())
    workload = build_workload()

    return render_template(
        "dashboard/admin.html",
        user_permissions=user_permissions,
        stats=stats,
        workload=workload
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

    for task in tasks:
        status_counts[task.status] = status_counts.get(task.status, 0) + 1

    overdue = [
        task for task in tasks
        if task.deadline
        and task.deadline < datetime.utcnow()
        and task.status != "Published"
    ]

    return {
        "total": len(tasks),
        "published": status_counts.get("Published", 0),
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
