from datetime import date
from calendar import month_name

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models import User, Client, ClientMonthlyTarget, ClientDeliverable,Task
from app.utils.permissions import has_permission


clients_bp = Blueprint("clients", __name__, url_prefix="/clients")


@clients_bp.route("/")
@login_required
def list_clients():

    if not has_permission(current_user, "manage_clients"):
        return redirect(url_for("dashboard.index"))

    search = request.args.get("q", "").strip()
    selected_status = request.args.get("status", "").strip()
    selected_manager = request.args.get("manager", "").strip()
    sort = request.args.get("sort", "newest").strip()

    query = Client.query

    if search:

        like = f"%{search}%"

        query = query.filter(
            db.or_(
                Client.client_name.ilike(like),
                Client.company_name.ilike(like),
                Client.industry.ilike(like),
            )
        )

    if selected_status:
        query = query.filter(Client.status == selected_status)

    if selected_manager.isdigit():
        query = query.filter(Client.assigned_manager_id == int(selected_manager))

    sort_options = {
        "newest": Client.id.desc(),
        "oldest": Client.id.asc(),
        "name_asc": Client.client_name.asc(),
        "name_desc": Client.client_name.desc(),
    }
    if sort not in sort_options:
        sort = "newest"
    query = query.order_by(sort_options[sort])

    page = request.args.get("page", 1, type=int)

    pagination = query.paginate(
        page=page,
        per_page=25,
        error_out=False
    )

    managers = User.query.filter(
        User.status == "active",
        User.role.in_(["admin", "super_admin"])
    ).order_by(User.name.asc()).all()

    is_filtered = bool(search or selected_status or selected_manager)

    return render_template(
        "clients/list.html",
        clients=pagination.items,
        pagination=pagination,
        search=search,
        selected_status=selected_status,
        selected_manager=selected_manager,
        sort=sort,
        managers=managers,
        is_filtered=is_filtered
    )


@clients_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_client():

    if not has_permission(current_user, "manage_clients"):
        return redirect(url_for("dashboard.index"))

    managers = User.query.filter(
        User.status == "active"
    ).order_by(User.name.asc()).all()

    if request.method == "POST":

        client = Client(
            client_name=request.form.get("client_name"),
            company_name=request.form.get("company_name"),
            phone=request.form.get("phone"),
            email=request.form.get("email"),
            industry=request.form.get("industry"),
            assigned_manager_id=request.form.get("assigned_manager_id") or None,
            status=request.form.get("status")
        )

        db.session.add(client)
        db.session.commit()

        flash("Client added successfully.", "success")
        return redirect(url_for("clients.list_clients"))

    return render_template(
        "clients/add.html",
        managers=managers
    )


@clients_bp.route("/<int:client_id>")
@login_required
def client_detail(client_id):

    if not has_permission(current_user, "manage_clients"):
        return redirect(url_for("dashboard.index"))

    client = Client.query.get_or_404(client_id)

    try:
        selected_month = int(request.args.get("month", date.today().month))
        selected_year = int(request.args.get("year", date.today().year))
    except (TypeError, ValueError):
        flash("Invalid month or year in the URL.", "error")
        return redirect(url_for("clients.client_detail", client_id=client_id))

    monthly_target = ClientMonthlyTarget.query.filter_by(
        client_id=client.id,
        month=selected_month,
        year=selected_year
    ).first()

    grouped_stats = {}

    if monthly_target:
        for item in monthly_target.deliverables:
            grouped_stats.setdefault(item.service_name, []).append(item)

    return render_template(
        "clients/detail.html",
        client=client,
        monthly_target=monthly_target,
        grouped_stats=grouped_stats,
        selected_month=selected_month,
        selected_year=selected_year,
        month_name=month_name[selected_month]
    )


@clients_bp.route("/<int:client_id>/deliverables/add", methods=["POST"])
@login_required
def add_deliverable(client_id):

    if not has_permission(current_user, "edit_monthly_targets"):
        return redirect(url_for("clients.client_detail", client_id=client_id))

    client = Client.query.get_or_404(client_id)

    try:
        month = int(request.form.get("month"))
        year = int(request.form.get("year"))
        completed_count = int(request.form.get("completed_count") or 0)
        target_count = int(request.form.get("target_count") or 0)

    except (TypeError, ValueError):
        flash(
            "Please provide valid numbers for month, year, "
            "completed count and target count.",
            "error"
        )
        return redirect(url_for("clients.client_detail", client_id=client_id))

    monthly_target = ClientMonthlyTarget.query.filter_by(
        client_id=client.id,
        month=month,
        year=year
    ).first()

    if not monthly_target:
        monthly_target = ClientMonthlyTarget(
            client_id=client.id,
            month=month,
            year=year
        )
        db.session.add(monthly_target)
        db.session.flush()

    deliverable = ClientDeliverable(
        monthly_target_id=monthly_target.id,
        service_name=request.form.get("service_name"),
        deliverable_name=request.form.get("deliverable_name"),
        completed_count=completed_count,
        target_count=target_count
    )

    db.session.add(deliverable)
    db.session.commit()

    flash("Deliverable added successfully.", "success")

    return redirect(
        url_for(
            "clients.client_detail",
            client_id=client.id,
            month=month,
            year=year
        )
    )

@clients_bp.route("/deliverable/<int:deliverable_id>/edit", methods=["GET", "POST"])
@login_required
def edit_deliverable(deliverable_id):

    if not has_permission(current_user, "edit_monthly_targets"):
        return redirect(url_for("dashboard.index"))

    deliverable = ClientDeliverable.query.get_or_404(deliverable_id)

    if request.method == "POST":

        try:
            completed_count = int(request.form.get("completed_count") or 0)
            target_count = int(request.form.get("target_count") or 0)

        except (TypeError, ValueError):
            flash(
                "Completed count and target count must be valid numbers.",
                "error"
            )
            return redirect(
                url_for(
                    "clients.edit_deliverable",
                    deliverable_id=deliverable.id
                )
            )

        deliverable.service_name = request.form.get("service_name")
        deliverable.deliverable_name = request.form.get("deliverable_name")
        deliverable.completed_count = completed_count
        deliverable.target_count = target_count

        db.session.commit()

        flash("Deliverable updated successfully.", "success")

        month_record = deliverable.monthly_target

        return redirect(
            url_for(
                "clients.client_detail",
                client_id=month_record.client_id,
                month=month_record.month,
                year=month_record.year
            )
        )

    return render_template(
        "clients/edit_deliverable.html",
        deliverable=deliverable
    )

@clients_bp.route("/deliverable/<int:deliverable_id>/delete")
@login_required
def delete_deliverable(deliverable_id):

    if not has_permission(current_user, "edit_monthly_targets"):
        return redirect(url_for("dashboard.index"))

    deliverable = ClientDeliverable.query.get_or_404(deliverable_id)

    month_record = deliverable.monthly_target

    linked_tasks = Task.query.filter_by(
        deliverable_id=deliverable.id
    ).count()

    if linked_tasks > 0:
        flash(
            "This deliverable cannot be deleted because tasks are linked to it.",
            "error"
        )

        return redirect(
            url_for(
                "clients.client_detail",
                client_id=month_record.client_id,
                month=month_record.month,
                year=month_record.year
            )
        )

    db.session.delete(deliverable)
    db.session.commit()

    flash("Deliverable deleted successfully.", "success")

    return redirect(
        url_for(
            "clients.client_detail",
            client_id=month_record.client_id,
            month=month_record.month,
            year=month_record.year
        )
    )