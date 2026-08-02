from datetime import date
from calendar import month_name

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)
from flask_login import login_required, current_user

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import User, Client, ClientMonthlyTarget, ClientDeliverable, Task, ClientAsset
from app.utils.permissions import (
    has_permission, can_manage_clients, can_view_client_stats,
)
from app.utils import roles
from app.utils import periods
from app.services import client_dashboard as client_dashboard_service
from app.services import deliverables
from app.utils.timezone import ist_now
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

    # Who may be set as a client's owning manager. Kept as its own name in
    # the role catalog rather than "whoever is an admin", because widening
    # it (a Senior Social Media Manager owning their own accounts, say) is
    # a policy call and should be made there, in one visible place.
    managers = User.query.filter(
        User.status == "active",
        User.role.in_(roles.CLIENT_MANAGER_ROLES)
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
            short_code=(request.form.get("short_code") or "").strip() or None,
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


@clients_bp.route("/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def edit_client(client_id):
    """Edit a client's details, owning manager, and active/inactive status.

    This is what makes the active/inactive filter and the inactive-asset read
    guard mean something: a client could previously only be made inactive AT
    creation. Parent re-parenting is deliberately excluded so the one-level,
    no-cycles sub-client invariant can't be broken from here.
    """
    if not can_manage_clients(current_user):
        return redirect(url_for("dashboard.index"))

    client = Client.query.get_or_404(client_id)

    # Only client-manager-tier users are offered (mirrors the list filter); the
    # current assignee is preserved even if they fall outside that set.
    managers = User.query.filter(
        User.status == "active",
        User.role.in_(roles.CLIENT_MANAGER_ROLES),
    ).order_by(User.name.asc()).all()

    if request.method == "POST":
        client_name = (request.form.get("client_name") or "").strip()
        if not client_name:
            flash("Client name is required.", "error")
            return redirect(url_for("clients.edit_client", client_id=client.id))

        status = request.form.get("status")
        if status not in ("active", "inactive"):
            status = client.status or "active"

        manager_id = (request.form.get("assigned_manager_id") or "").strip()
        if manager_id:
            try:
                manager_id = int(manager_id)
            except (TypeError, ValueError):
                manager_id = None
        else:
            manager_id = None

        client.client_name = client_name
        client.short_code = (request.form.get("short_code") or "").strip() or None
        client.company_name = (request.form.get("company_name") or "").strip() or None
        client.phone = (request.form.get("phone") or "").strip() or None
        client.email = (request.form.get("email") or "").strip() or None
        client.industry = (request.form.get("industry") or "").strip() or None
        # Brand knowledge base - free text the AI assist reads for on-brand
        # captions/QA. Empty clears it (falls back to industry + brief).
        client.brand_voice = (
            request.form.get("brand_voice") or "").strip() or None
        client.brand_guidelines_notes = (
            request.form.get("brand_guidelines_notes") or "").strip() or None
        # Structured Client Brain (AI knowledgebase / fact-check source).
        from app.ai import client_brain
        client.brand_brain = client_brain.from_form(request.form)
        client.brand_offers = client_brain.offers_from_form(request.form)
        # Guarded auto-reply opt-in for this client's Google reviews.
        client.gmb_autoreply = bool(request.form.get("gmb_autoreply"))
        client.comment_autoreply = bool(request.form.get("comment_autoreply"))
        client.assigned_manager_id = manager_id
        client.status = status
        db.session.commit()

        flash("Client updated." + (" It is now inactive."
              if status == "inactive" else ""), "success")
        return redirect(url_for("clients.client_detail", client_id=client.id))

    from app.ai import client_brain
    return render_template(
        "clients/edit.html", client=client, managers=managers,
        brain_sections=client_brain.SECTIONS,
        brain=client.brand_brain or {},
        offers=client_brain.offers_display(client),
        max_offers=client_brain.MAX_OFFERS)


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
        # ist_now(), not date.today(): the server runs on UTC, so on the 1st of
        # a month before IST 05:30 this opened the client on LAST month's
        # targets and deliverables - the one moment in the month when someone
        # is most likely to be checking whether the new month is set up.
        _today = ist_now().date()
        selected_month = int(request.args.get("month", _today.month))
        selected_year = int(request.args.get("year", _today.year))
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
        # Illustrative only - shows the short code in the shape it will
        # actually appear in, rather than describing it in prose.
        task_code_example="1277",
        today_ddmmyy=ist_now().strftime("%d-%m-%y"),
    )


@clients_bp.route("/<int:client_id>/dashboard")
@login_required
def client_dashboard(client_id):
    """Delivery figures for one client.

    A sibling route rather than a tab toggled on the detail page: the
    aggregates would otherwise run on every client view whether or not
    anyone looked at them, the period picker needs query args anyway, and
    this way the window someone is looking at has a shareable URL.
    """
    if not can_view_client_stats(current_user):
        abort(403)

    client = Client.query.get_or_404(client_id)

    # Defaults to the calendar month, because "how much have we delivered
    # this month" is the question this page exists for. All-time is offered
    # too - a client's whole history is a fair thing to ask for.
    period = periods.resolve_period(request.args, allow_all=True,
                                    default="month")

    return render_template(
        "clients/dashboard.html",
        client=client,
        period=period,
        data=client_dashboard_service.build_dashboard(client, period),
        format_seconds=_format_duration,
    )


def _format_duration(seconds):
    """Seconds -> "3h 20m" / "2.4d". None when there was nothing to average."""
    if not seconds:
        return "—"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h".replace(".0h", "h")
    return f"{hours / 24:.1f}d".replace(".0d", "d")


@clients_bp.route("/<int:client_id>/short-code", methods=["POST"])
@login_required
def update_client_short_code(client_id):
    """Set the client's short code from the client page.

    There is no full edit-client screen, and the code is needed on
    clients that already exist (every client predates the field), so
    it is editable in place on the one page that already shows it.
    """

    if not can_manage_clients(current_user):
        return redirect(url_for("dashboard.index"))

    client = Client.query.get_or_404(client_id)

    short_code = (request.form.get("short_code") or "").strip()

    if len(short_code) > 12:
        flash("Short code must be 12 characters or fewer.", "error")
        return redirect(url_for("clients.client_detail", client_id=client.id))

    client.short_code = short_code or None
    db.session.commit()

    if short_code:
        flash(f"Short code set to {client.code}.", "success")
    else:
        flash(
            f"Short code cleared - falling back to {client.code}.",
            "success",
        )

    return redirect(url_for("clients.client_detail", client_id=client.id))


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

    # A month outside 1-12 (or a wild year) produces a deliverable that
    # client_detail can never render again; counts must be non-negative.
    if not (1 <= month <= 12) or not (2000 <= year <= 2100):
        flash("Pick a valid month (1-12) and year.", "error")
        return redirect(url_for("clients.client_detail", client_id=client_id))
    completed_count = max(0, completed_count)
    target_count = max(0, target_count)

    # service_name / deliverable_name are NOT NULL - a blank submit would 500.
    service_name = (request.form.get("service_name") or "").strip()
    deliverable_name = (request.form.get("deliverable_name") or "").strip()
    if not service_name or not deliverable_name:
        flash("Service and deliverable name are required.", "error")
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
        try:
            db.session.flush()
        except IntegrityError:
            # A concurrent submit created this month's target first and the
            # unique (client, month, year) constraint tripped. Reuse theirs
            # rather than 500 - the deliverable still lands on the right month.
            db.session.rollback()
            monthly_target = ClientMonthlyTarget.query.filter_by(
                client_id=client.id,
                month=month,
                year=year
            ).first()

    deliverable = ClientDeliverable(
        monthly_target_id=monthly_target.id,
        service_name=service_name,
        deliverable_name=deliverable_name,
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

        # NOT NULL names, and non-negative counts (a negative gives a false
        # progress % and a spurious "drift" flag on the client dashboard).
        service_name = (request.form.get("service_name") or "").strip()
        deliverable_name = (request.form.get("deliverable_name") or "").strip()
        if not service_name or not deliverable_name:
            flash("Service and deliverable name are required.", "error")
            return redirect(url_for(
                "clients.edit_deliverable", deliverable_id=deliverable.id))

        deliverable.service_name = service_name
        deliverable.deliverable_name = deliverable_name
        deliverable.target_count = max(0, target_count)

        # Under the row lock like every other writer of this column. A human
        # typing an absolute number still races the publish worker: without
        # the lock, a delivery that lands while this form is open is silently
        # overwritten by whatever the count was when the page rendered.
        deliverables.set_count(deliverable.id, completed_count)

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

@clients_bp.route("/deliverable/<int:deliverable_id>/delete", methods=["POST"])
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


#: One file per request, for the upload popup.
#:
#: add_client_asset above posts the whole batch in a single request, and
#: every file in it costs a round trip to storage - roughly a second even
#: for a 50 KB logo. Thirty or forty assets is therefore a 45-second-plus
#: request, which the proxy in front of the app kills long before it
#: finishes; and because the whole batch shares one transaction, a failure
#: near the end rolls back every row while leaving the objects already
#: pushed to storage behind as orphans.
#:
#: These three routes split that up. Nothing here handles more than one
#: file, so no single request can time out, hit the body-size cap, or take
#: the rest of the batch down with it. The old route stays as the no-JS
#: path.


def _asset_upload_guard(client_id):
    """Shared gate: the same permission add_client_asset uses, as JSON."""
    if not can_manage_clients(current_user):
        return None, (jsonify(
            success=False,
            message="You are not allowed to upload assets for this client.",
        ), 403)
    return Client.query.get_or_404(client_id), None


@clients_bp.route("/<int:client_id>/assets/upload-one", methods=["POST"])
@login_required
def upload_one_client_asset(client_id):
    """Store exactly one asset. Deliberately quiet - no flash, because the
    batch is summed up once by commit_client_assets."""
    client, error = _asset_upload_guard(client_id)
    if error:
        return error

    category = (request.form.get("category") or "").strip()
    if not client_asset_catalog.is_valid(category):
        return jsonify(success=False, message="Select a valid asset type."), 400

    uploaded = request.files.get("file")
    if not uploaded or not (uploaded.filename or "").strip():
        return jsonify(success=False, message="No file provided."), 400

    try:
        result = StorageService().upload_client_asset(
            client=client,
            file_storage=uploaded,
            uploaded_by_id=current_user.id,
            category=category,
        )
        db.session.commit()

    except StorageServiceError as error:
        db.session.rollback()
        current_app.logger.exception(
            "Unable to upload brand asset for client %s.", client.id)
        # The real reason, not "please try again" - an invalid category or
        # an unnamed file is something the person can actually act on.
        return jsonify(success=False, message=str(error)), 400

    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Unable to upload brand asset for client %s.", client.id)
        return jsonify(
            success=False,
            message="Upload failed — please try again.",
        ), 500

    return jsonify(success=True, file_id=result["asset"].id)


@clients_bp.route("/<int:client_id>/assets/discard/<int:asset_id>",
                  methods=["POST"])
@login_required
def discard_client_asset(client_id, asset_id):
    """Undo one upload from the popup - the x, or closing it.

    Narrower than delete_client_asset: this is only for an asset this
    person just uploaded and has not committed, so it skips the flashes
    and the redirect. The Social Studio check still applies, because an
    asset can in principle be picked up between upload and cancel.
    """
    client, error = _asset_upload_guard(client_id)
    if error:
        return error

    asset = ClientAsset.query.filter_by(
        id=asset_id,
        client_id=client.id,
        uploaded_by_id=current_user.id,
    ).first()

    if asset is None:
        return jsonify(
            success=False,
            message="That asset is not one of yours to discard.",
        ), 404

    if current_app.config.get("SOCIAL_ENGINE_ENABLED"):
        from app.models import SocialMediaAsset
        if SocialMediaAsset.query.filter_by(client_asset_id=asset.id).first():
            return jsonify(
                success=False,
                message="This asset is already used in a Social Studio post.",
            ), 409

    object_key = asset.object_key

    try:
        db.session.delete(asset)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Unable to discard client asset %s.", asset_id)
        return jsonify(
            success=False,
            message="Could not discard the asset.",
        ), 500

    # Row first, object second: an orphaned object is swept up later,
    # whereas a row pointing at a deleted object is a broken tile.
    try:
        StorageService().delete(object_key=object_key)
    except Exception:  # noqa: BLE001
        current_app.logger.warning(
            "Discarded asset %s but could not delete %s", asset_id, object_key)

    return jsonify(success=True)


@clients_bp.route("/<int:client_id>/assets/commit", methods=["POST"])
@login_required
def commit_client_assets(client_id):
    """Done. The uploads are already stored and committed one by one, so
    all this does is say how many landed - the message the old single-post
    route flashed at the end."""
    client, error = _asset_upload_guard(client_id)
    if error:
        return error

    data = request.get_json(silent=True) or {}
    file_ids = [i for i in (data.get("file_ids") or []) if isinstance(i, int)]

    # Counted from the database rather than trusted from the browser, so
    # the message cannot claim more than actually exists.
    count = ClientAsset.query.filter(
        ClientAsset.id.in_(file_ids or [-1]),
        ClientAsset.client_id == client.id,
    ).count() if file_ids else 0

    if not count:
        return jsonify(success=False, message="Nothing to add."), 400

    flash(f"{count} asset(s) added.", "success")
    return jsonify(success=True)


def _asset_readable(asset):
    """May the signed-in user fetch this brand asset?

    Reading a client's assets is deliberately open to the whole team - the
    logo, fonts and guidelines are what everyone needs to do creative work
    (see client_detail). What was not intended is that these two routes
    took an id and nothing else, so an asset belonging to an INACTIVE
    client - one the list deliberately hides from everyone but a manager -
    was still fetchable by anybody who guessed the number.
    """
    client = asset.client if asset is not None else None

    if client is None:
        return can_manage_clients(current_user)

    if (client.status or "active") == "active":
        return True

    return can_manage_clients(current_user)


@clients_bp.route("/assets/<int:asset_id>/preview")
@login_required
def preview_client_asset(asset_id):

    asset = ClientAsset.query.get_or_404(asset_id)

    if not _asset_readable(asset):
        flash("That asset is not available.", "error")
        return redirect(url_for("clients.list_clients"))

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

    if not _asset_readable(asset):
        flash("That asset is not available.", "error")
        return redirect(url_for("clients.list_clients"))

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

    # A brand asset pulled into a Social Studio post is referenced by a
    # SocialMediaAsset (FK, no cascade); deleting it would fail with a bare
    # IntegrityError caught below as a misleading "try again". Detect + explain.
    if current_app.config.get("SOCIAL_ENGINE_ENABLED"):
        from app.models import SocialMediaAsset
        if SocialMediaAsset.query.filter_by(client_asset_id=asset.id).first():
            flash("This asset is used in a Social Studio post — remove it from "
                  "the post (or delete the post) before deleting the asset.",
                  "error")
            return redirect(url_for("clients.client_detail", client_id=client_id))

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