from datetime import date
from calendar import month_name

from flask import (
    Blueprint,
    current_app,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)
from flask_login import login_required, current_user

from app.extensions import db
from app.models import User, Client, ClientMonthlyTarget, ClientDeliverable, Task, ClientAsset
from app.utils.permissions import has_permission, can_manage_clients
from app.utils import client_assets as client_asset_catalog
from app.storage.storage_service import StorageService, StorageServiceError


clients_bp = Blueprint("clients", __name__, url_prefix="/clients")


def _can_edit_deliverables(user):
    """Who may add/edit/remove a client's monthly deliverables.

    edit_monthly_targets is kept as its own grant path so a non-admin
    who was deliberately given that permission does not lose it here.
    """
    return (
        can_manage_clients(user)
        or has_permission(user, "edit_monthly_targets")
    )


@clients_bp.route("/")
@login_required
def list_clients():

    # Readable by everyone: this is how the team reaches a client's
    # brand assets. Employees get a reduced table (name + manager
    # only) - see the can_manage flag passed to the template.
    can_manage = can_manage_clients(current_user)

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

    # Status is a curation concern; an employee browsing for brand
    # assets should simply never be shown inactive clients rather than
    # being handed a filter for them.
    if can_manage:
        if selected_status:
            query = query.filter(Client.status == selected_status)
    else:
        selected_status = ""
        query = query.filter(Client.status == "active")

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
        is_filtered=is_filtered,
        can_manage=can_manage,
    )


@clients_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_client():

    if not can_manage_clients(current_user):
        return redirect(url_for("dashboard.index"))

    managers = User.query.filter(
        User.status == "active"
    ).order_by(User.name.asc()).all()

    # Only top-level clients are offered as a parent - a sub-client
    # cannot itself have sub-clients, so the hierarchy never goes past
    # one level.
    parent_options = Client.query.filter_by(
        status="active",
        parent_client_id=None
    ).order_by(Client.client_name.asc()).all()

    if request.method == "POST":

        client_name = (request.form.get("client_name") or "").strip()

        # client_name is NOT NULL; without this an empty submit reached the
        # DB and raised an IntegrityError (500) instead of a clean message.
        if not client_name:
            flash("Client name is required.", "error")
            return redirect(url_for("clients.add_client"))

        parent_client_id = None
        parent_raw = (request.form.get("parent_client_id") or "").strip()

        if parent_raw:

            try:
                parent_client_id = int(parent_raw)
            except ValueError:
                flash("Invalid parent client selected.", "error")
                return redirect(url_for("clients.add_client"))

            parent_client = Client.query.filter_by(
                id=parent_client_id,
                status="active",
                parent_client_id=None
            ).first()

            if not parent_client:
                flash(
                    "Selected parent client is invalid - it must be an "
                    "existing top-level client.",
                    "error"
                )
                return redirect(url_for("clients.add_client"))

        client = Client(
            client_name=client_name,
            company_name=request.form.get("company_name"),
            phone=request.form.get("phone"),
            email=request.form.get("email"),
            industry=request.form.get("industry"),
            assigned_manager_id=request.form.get("assigned_manager_id") or None,
            status=request.form.get("status"),
            parent_client_id=parent_client_id,
        )

        db.session.add(client)
        db.session.commit()

        if parent_client_id:
            flash(
                f"Sub-client added under {client.parent.client_name}.",
                "success"
            )
            return redirect(
                url_for("clients.client_detail", client_id=parent_client_id)
            )

        flash("Client added successfully.", "success")
        return redirect(url_for("clients.list_clients"))

    preselected_parent_id = request.args.get("parent_id", type=int)

    return render_template(
        "clients/add.html",
        managers=managers,
        parent_options=parent_options,
        preselected_parent_id=preselected_parent_id,
    )


@clients_bp.route("/<int:client_id>")
@login_required
def client_detail(client_id):

    # Viewing is open to anyone signed in - the brand assets here
    # (logo, fonts, guidelines) are what the whole team reaches for
    # to do creative work on this client, not just managers.
    #
    # What each role actually gets is decided by the two flags below
    # and enforced again on every write route: an employee sees the
    # client's name, its manager and the brand assets; the client's
    # own contact details, its deliverables and its sub-clients are
    # curation surface and stay with admins/super-admins.
    client = Client.query.get_or_404(client_id)

    can_manage = can_manage_clients(current_user)
    can_edit_deliverables = _can_edit_deliverables(current_user)

    try:
        selected_month = int(request.args.get("month", date.today().month))
        selected_year = int(request.args.get("year", date.today().year))
        # month_name is indexed 1..12 below; an out-of-range month parses
        # fine as an int but would raise IndexError, so reject it here.
        if not (1 <= selected_month <= 12):
            raise ValueError("month out of range")
    except (TypeError, ValueError):
        flash("Invalid month or year in the URL.", "error")
        return redirect(url_for("clients.client_detail", client_id=client_id))

    # Deliverables are manager-only surface, so an employee's page load
    # skips the query outright rather than fetching rows the template
    # will not render.
    monthly_target = None
    grouped_stats = {}

    if can_edit_deliverables:

        monthly_target = ClientMonthlyTarget.query.filter_by(
            client_id=client.id,
            month=selected_month,
            year=selected_year
        ).first()

        if monthly_target:
            for item in monthly_target.deliverables:
                grouped_stats.setdefault(item.service_name, []).append(item)

    # Grouped in catalog order (Logo first, then Brand Image, Video...)
    # rather than upload order, so the asset library reads the same way
    # every time regardless of what was added when.
    assets_by_category = {}

    for asset in client.assets:
        assets_by_category.setdefault(asset.category, []).append(asset)

    # Most recently uploaded logo, if any, shown next to the client's
    # name the way a company logo sits next to its name in any CRM -
    # None (never an exception) is exactly what the template needs to
    # fall back to the plain icon, same contract as avatar_url().
    logo_asset = next(iter(assets_by_category.get("logo", [])), None)
    client_logo_url = None

    if logo_asset:
        try:
            client_logo_url = StorageService().preview_url(
                object_key=logo_asset.object_key
            )
        except StorageServiceError:
            current_app.logger.exception(
                "Unable to generate preview URL for client logo %s.",
                logo_asset.id,
            )

    return render_template(
        "clients/detail.html",
        client_logo_url=client_logo_url,
        client=client,
        monthly_target=monthly_target,
        grouped_stats=grouped_stats,
        selected_month=selected_month,
        selected_year=selected_year,
        month_name=month_name[selected_month],
        asset_categories=client_asset_catalog.CATEGORIES,
        assets_by_category=assets_by_category,
        can_manage=can_manage,
        can_edit_deliverables=can_edit_deliverables,
    )


@clients_bp.route("/<int:client_id>/deliverables/add", methods=["POST"])
@login_required
def add_deliverable(client_id):

    if not _can_edit_deliverables(current_user):
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

    if not _can_edit_deliverables(current_user):
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

    if not _can_edit_deliverables(current_user):
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


# ===========================
# Client brand assets
#
# Permanent, client-level files - logo, brand imagery, video, fonts,
# brand guideline docs - shown on the client page rather than any one
# task. Anyone signed in may view/download them (the whole team needs
# the client's logo and fonts to actually do creative work); only
# manage_clients may upload or delete, matching who curates the rest
# of the client record.
# ===========================

@clients_bp.route("/<int:client_id>/assets/add", methods=["POST"])
@login_required
def add_client_asset(client_id):

    if not can_manage_clients(current_user):
        return redirect(url_for("dashboard.index"))

    client = Client.query.get_or_404(client_id)

    category = (request.form.get("category") or "").strip()

    if not client_asset_catalog.is_valid(category):
        flash("Select a valid asset type.", "error")
        return redirect(
            url_for("clients.client_detail", client_id=client.id)
        )

    uploaded_files = [
        f for f in request.files.getlist("asset_files")
        if f and (f.filename or "").strip()
    ]

    if not uploaded_files:
        flash("Choose at least one file to upload.", "error")
        return redirect(
            url_for("clients.client_detail", client_id=client.id)
        )

    storage = StorageService()
    uploaded_count = 0

    try:
        for file_storage in uploaded_files:
            storage.upload_client_asset(
                client=client,
                file_storage=file_storage,
                uploaded_by_id=current_user.id,
                category=category,
            )
            uploaded_count += 1

        db.session.commit()

    except StorageServiceError:
        db.session.rollback()

        current_app.logger.exception(
            "Unable to upload brand asset(s) for client %s.",
            client.id,
        )

        flash(
            "Unable to upload the file(s). Please try again.",
            "error",
        )

        return redirect(
            url_for("clients.client_detail", client_id=client.id)
        )

    flash(
        f"{uploaded_count} asset(s) added to {client_asset_catalog.label(category)}.",
        "success",
    )

    return redirect(
        url_for("clients.client_detail", client_id=client.id)
    )


@clients_bp.route("/assets/<int:asset_id>/preview")
@login_required
def preview_client_asset(asset_id):

    asset = ClientAsset.query.get_or_404(asset_id)

    try:
        preview_url = StorageService().preview_url(
            object_key=asset.object_key,
            expires_in=600,
        )

    except StorageServiceError:
        current_app.logger.exception(
            "Unable to generate preview URL for client asset %s.",
            asset.id,
        )

        flash("Asset preview is currently unavailable.", "error")

        return redirect(
            url_for("clients.client_detail", client_id=asset.client_id)
        )

    return redirect(preview_url)


@clients_bp.route("/assets/<int:asset_id>/download")
@login_required
def download_client_asset(asset_id):

    asset = ClientAsset.query.get_or_404(asset_id)

    try:
        download_url = StorageService().download_url(
            object_key=asset.object_key,
            download_filename=asset.original_filename,
            expires_in=600,
        )

    except StorageServiceError:
        current_app.logger.exception(
            "Unable to generate download URL for client asset %s.",
            asset.id,
        )

        flash("Asset download is currently unavailable.", "error")

        return redirect(
            url_for("clients.client_detail", client_id=asset.client_id)
        )

    return redirect(download_url)


@clients_bp.route("/assets/<int:asset_id>/delete", methods=["POST"])
@login_required
def delete_client_asset(asset_id):

    asset = ClientAsset.query.get_or_404(asset_id)
    client_id = asset.client_id

    if not can_manage_clients(current_user):
        flash("You are not allowed to delete this asset.", "error")
        return redirect(
            url_for("clients.client_detail", client_id=client_id)
        )

    filename = asset.original_filename
    object_key = asset.object_key

    try:
        db.session.delete(asset)
        db.session.commit()

    except Exception:
        db.session.rollback()

        current_app.logger.exception(
            "Unable to delete client asset %s.",
            asset_id,
        )

        flash("Unable to delete the asset. Please try again.", "error")

        return redirect(
            url_for("clients.client_detail", client_id=client_id)
        )

    try:
        StorageService().delete(object_key=object_key)
    except Exception:
        current_app.logger.exception(
            "Unable to remove storage object for deleted client asset "
            "%s: %s",
            asset_id, object_key,
        )

    flash(f'"{filename}" deleted.', "success")

    return redirect(
        url_for("clients.client_detail", client_id=client_id)
    )