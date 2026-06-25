from datetime import date

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash
)

from flask_login import (
    login_required,
    current_user
)

from app.extensions import db

from app.models import (
    DailyReport,
    Task
)

from app.utils.permissions import has_permission


reports_bp = Blueprint(
    "reports",
    __name__,
    url_prefix="/reports"
)


@reports_bp.route("/")
@login_required
def list_reports():

    if has_permission(current_user, "view_reports"):

        reports = DailyReport.query.order_by(
            DailyReport.report_date.desc()
        ).all()

    else:

        reports = DailyReport.query.filter_by(
            employee_id=current_user.id
        ).order_by(
            DailyReport.report_date.desc()
        ).all()

    return render_template(
        "reports/list.html",
        reports=reports
    )


@reports_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_report():

    tasks = Task.query.filter_by(
        assigned_to_id=current_user.id
    ).all()

    if request.method == "POST":

        report = DailyReport(
            employee_id=current_user.id,
            task_id=request.form.get("task_id"),
            report_date=date.today(),
            completed_work=request.form.get("completed_work"),
            hours_worked=float(
                request.form.get("hours_worked") or 0
            ),
            in_progress_work=request.form.get(
                "in_progress_work"
            ),
            issues=request.form.get("issues"),
            tomorrow_plan=request.form.get(
                "tomorrow_plan"
            )
        )

        db.session.add(report)
        db.session.commit()

        flash(
            "Report submitted successfully.",
            "success"
        )

        return redirect(
            url_for("reports.list_reports")
        )

    return render_template(
        "reports/add.html",
        tasks=tasks
    )

@reports_bp.route("/<int:report_id>")
@login_required
def view_report(report_id):

    report = DailyReport.query.get_or_404(report_id)

    if (
        report.employee_id != current_user.id
        and
        not has_permission(current_user, "view_reports")
    ):
        return redirect(url_for("reports.list_reports"))

    return render_template(
        "reports/view.html",
        report=report
    )