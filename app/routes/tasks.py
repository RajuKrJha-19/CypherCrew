import os
from werkzeug.utils import secure_filename
from app.utils.timezone import ist_now
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
    jsonify,
)

from flask_login import login_required, current_user

from sqlalchemy import or_, cast, String
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.extensions import db
from app.models import (
    Task,
    Client,
    ClientDeliverable,
    User,
    TaskFeedback,
    TaskActivity,
    TaskSequence,
    TaskComment,
    TaskFile
)
from app.utils.permissions import has_permission
from app.utils.notifications import create_notification
from app.utils import task_status


tasks_bp = Blueprint("tasks", __name__, url_prefix="/tasks")

from app.storage.storage_service import (
    StorageService,
    StorageServiceError,
)

from app.services import thumbnails

from app.models import TaskFile

#: How long a signed thumbnail URL - and the browser's cache of it -
#: stays valid. Longer than a preview URL because a thumbnail is small,
#: immutable for a given file, and requested dozens at a time.
THUMBNAIL_URL_TTL = 3600


def generate_task_code():

    # Locked read, so two people saving a task at the same moment queue up
    # instead of both reading the same last_code and handing out one number
    # twice.
    sequence = db.session.get(
        TaskSequence,
        1,
        with_for_update=True,
    )

    if not sequence:

        # First task ever on this database. ON CONFLICT DO NOTHING because a
        # concurrent request may be inserting the very same seed row - losing
        # that race is fine, we just re-read what the winner wrote.
        db.session.execute(
            pg_insert(TaskSequence.__table__)
            .values(id=1, last_code=1000)
            .on_conflict_do_nothing(index_elements=["id"])
        )

        sequence = db.session.get(
            TaskSequence,
            1,
            with_for_update=True,
        )

    sequence.last_code += 1

    return sequence.last_code


def pause_timer(task):

    if task.timer_started_at:

        elapsed = datetime.utcnow() - task.timer_started_at

        task.worked_seconds = (
            task.worked_seconds or 0
        ) + int(
            elapsed.total_seconds()
        )

        task.timer_started_at = None


def start_timer(task):

    now = datetime.utcnow()

    if not task.started_at:
        task.started_at = now

    task.timer_started_at = now


def record_status_time(task, new_status):

    now = datetime.utcnow()
    old_status = task.status

    if not task.status_started_at:

        task.status_started_at = now
        task.status = new_status

        return old_status

    elapsed = int(
        (now - task.status_started_at).total_seconds()
    )

    # Driven by the status table rather than an if/elif chain, so a
    # new status can never be added without a bucket to bank its time
    # in - that is exactly how the old "Hold" status silently dropped
    # every second a task spent in it.
    field = task_status.duration_field(task.status)

    if field:
        setattr(
            task,
            field,
            (getattr(task, field) or 0) + elapsed
        )

    # Leaving On Hold or Void must drop the reason that put it there.
    # Every status change funnels through here, so doing it at this
    # one point stops a stale reason surviving into the next status.
    if task.status == task_status.ON_HOLD \
            and new_status != task_status.ON_HOLD:
        task.hold_reason = None
        task.held_at = None
        task.held_by_id = None

    if task.status == task_status.VOID \
            and new_status != task_status.VOID:
        task.void_reason = None
        task.voided_at = None
        task.voided_by_id = None

    task.status = new_status
    task.status_started_at = now

    return old_status


def format_seconds(seconds):

    seconds = seconds or 0

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    return f"{hours} hr {minutes} min"


def get_live_worked_seconds(task):

    total = task.worked_seconds or 0

    if task.timer_started_at:
        total += int(
            (datetime.utcnow() - task.timer_started_at).total_seconds()
        )

    return total

def build_task_update_message(changes):

    if not changes:
        return f"Task updated by {current_user.name}"

    message = f"Task updated by {current_user.name}\n\nChanges:\n"

    for label, old_value, new_value in changes:
        message += f"\n{label}\n{old_value or '-'} â†’ {new_value or '-'}\n"

    return message

def add_activity(
    task,
    action,
    message=None,
    old_status=None,
    new_status=None
):

    activity = TaskActivity(
        task_id=task.id,
        actor_id=current_user.id,
        action=action,
        message=message,
        old_status=old_status,
        new_status=new_status,
        created_at=datetime.utcnow()
    )

    db.session.add(activity)


def apply_task_search(query, search):

    if not search:
        return query

    clean_search = search.strip().replace("#", "")

    return query.join(
        Client,
        Task.client_id == Client.id
    ).join(
        ClientDeliverable,
        Task.deliverable_id == ClientDeliverable.id
    ).outerjoin(
        User,
        Task.assigned_to_id == User.id
    ).filter(
        or_(
            Task.title.ilike(f"%{search}%"),
            Task.description.ilike(f"%{search}%"),
            Task.status.ilike(f"%{search}%"),
            Task.priority.ilike(f"%{search}%"),

            cast(Task.task_code, String).ilike(
                f"%{clean_search}%"
            ),

            Client.client_name.ilike(f"%{search}%"),

            ClientDeliverable.service_name.ilike(f"%{search}%"),
            ClientDeliverable.deliverable_name.ilike(f"%{search}%"),

            User.name.ilike(f"%{search}%"),
            User.email.ilike(f"%{search}%")
        )
    )


def get_task_base_query():

    if has_permission(current_user, "manage_tasks"):
        return Task.query

    return Task.query.filter(
        db.or_(
            Task.assigned_to_id == current_user.id,
            Task.visible_to.any(User.id == current_user.id)
        )
    )


def apply_task_filters(query, args):
    """Status / priority / search / date-range / assignee / client
    filters, shared by the task list and its live-refresh endpoint so
    the poll always scopes to exactly what the page is showing. Sorting
    is deliberately left out - it belongs only to the rendered page."""

    selected_status = args.get("status", "").strip()
    selected_priority = args.get("priority", "").strip()
    search = args.get("q", "").strip()
    filter_by = args.get("filter", "").strip()
    assigned_to = args.get("assigned_to", "").strip()
    assigned_by = args.get("assigned_by", "").strip()
    client_id = args.get("client", "").strip()

    if selected_status:
        query = query.filter(Task.status == selected_status)

    if selected_priority:
        query = query.filter(Task.priority == selected_priority)

    query = apply_task_search(query, search)

    today = ist_now()

    if filter_by == "today":
        query = query.filter(db.func.date(Task.created_at) == today.date())

    elif filter_by == "yesterday":
        query = query.filter(
            db.func.date(Task.created_at) == today.date() - timedelta(days=1)
        )

    elif filter_by == "last_7_days":
        query = query.filter(Task.created_at >= today - timedelta(days=7))

    elif filter_by == "last_30_days":
        query = query.filter(Task.created_at >= today - timedelta(days=30))

    elif filter_by == "this_month":
        query = query.filter(
            db.extract("month", Task.created_at) == today.month,
            db.extract("year", Task.created_at) == today.year
        )

    elif filter_by == "last_90_days":
        query = query.filter(Task.created_at >= today - timedelta(days=90))

    elif filter_by == "custom_days":
        custom_days_value = args.get("days", "").strip()
        if custom_days_value.isdigit() and int(custom_days_value) > 0:
            query = query.filter(
                Task.created_at >= today - timedelta(days=int(custom_days_value))
            )

    if assigned_to and assigned_to.isdigit():
        query = query.filter(Task.assigned_to_id == int(assigned_to))

    if assigned_by and assigned_by.isdigit():
        query = query.filter(Task.created_by_id == int(assigned_by))

    if client_id and client_id.isdigit():
        query = query.filter(Task.client_id == int(client_id))

    return query


@tasks_bp.route("/live-state")
@login_required
def live_state():
    """Compact, filtered snapshot of the visible tasks, polled by the
    board / list live-refresh. One columns-only query - no joins, no ORM
    object hydration - so it stays cheap at a ~10s cadence. The client
    reconciles card moves, removals and new arrivals from this; it never
    re-renders the page.

    Scoped by the same filters and the same permission base query as the
    list, so the poll returns exactly the set the page is showing."""

    query = apply_task_filters(get_task_base_query(), request.args)

    rows = query.with_entities(
        Task.id,
        Task.status,
        Task.priority,
        Task.employee_completed,
        Task.deadline,
    ).all()

    now = ist_now()

    tasks = {}
    completed = review = overdue = 0

    for task_id, status, priority, employee_completed, deadline in rows:

        # Void tasks aren't shown on the board and are excluded from the
        # headline figures - matching list_tasks().
        is_void = status in task_status.EXCLUDED_FROM_METRICS

        tasks[str(task_id)] = {"status": status, "priority": priority, "void": is_void}

        if is_void:
            continue

        if employee_completed:
            completed += 1

        if status in (task_status.CORE_REVIEW, task_status.CLIENT_REVIEW):
            review += 1

        if (
            deadline
            and deadline < now
            and status in (
                task_status.ASSIGNED,
                task_status.IN_PROGRESS,
                task_status.PAUSED,
            )
        ):
            overdue += 1

    non_void = sum(1 for t in tasks.values() if not t["void"])

    return jsonify(
        tasks=tasks,
        counts={
            "total": non_void,
            "completed": completed,
            "review": review,
            "overdue": overdue,
        },
    )


@tasks_bp.route("/")
@login_required
def list_tasks():

    selected_status = request.args.get("status", "").strip()
    selected_priority = request.args.get("priority", "").strip()
    search = request.args.get("q", "").strip()

    sort_by = request.args.get("sort", "").strip()
    filter_by = request.args.get("filter", "").strip()
    assigned_to = request.args.get("assigned_to", "").strip()
    assigned_by = request.args.get("assigned_by", "").strip()
    client_id = request.args.get("client", "").strip()

    query = apply_task_filters(get_task_base_query(), request.args)

        # =====================================
    # SORT BY
    # =====================================

    if sort_by == "oldest":

        query = query.order_by(Task.id.asc())

    elif sort_by == "deadline_asc":

        query = query.order_by(
            Task.deadline.asc().nullslast(),
            Task.id.desc()
        )

    elif sort_by == "deadline_desc":

        query = query.order_by(
            Task.deadline.desc().nullslast(),
            Task.id.desc()
        )

    elif sort_by == "priority_high":

        query = query.order_by(
            db.case(
                (Task.priority == "Urgent", 4),
                (Task.priority == "High", 3),
                (Task.priority == "Medium", 2),
                (Task.priority == "Low", 1),
                else_=0
            ).desc()
        )

    elif sort_by == "priority_low":

        query = query.order_by(
            db.case(
                (Task.priority == "Urgent", 4),
                (Task.priority == "High", 3),
                (Task.priority == "Medium", 2),
                (Task.priority == "Low", 1),
                else_=0
            ).asc()
        )

    elif sort_by == "taskid_asc":

        query = query.order_by(Task.task_code.asc())

    elif sort_by == "taskid_desc":

        query = query.order_by(Task.task_code.desc())

    elif sort_by == "title_asc":

        query = query.order_by(Task.title.asc())

    elif sort_by == "title_desc":

        query = query.order_by(Task.title.desc())

    elif sort_by == "file_size_desc":

        file_size_sum = db.func.coalesce(db.func.sum(TaskFile.file_size), 0)

        query = (
            query
            .outerjoin(
                TaskFile,
                db.and_(
                    TaskFile.task_id == Task.id,
                    TaskFile.folder_type == "submission"
                )
            )
            .group_by(Task.id)
            .order_by(file_size_sum.desc())
        )

    elif sort_by == "file_size_asc":

        file_size_sum = db.func.coalesce(db.func.sum(TaskFile.file_size), 0)

        query = (
            query
            .outerjoin(
                TaskFile,
                db.and_(
                    TaskFile.task_id == Task.id,
                    TaskFile.folder_type == "submission"
                )
            )
            .group_by(Task.id)
            .order_by(file_size_sum.asc())
        )

    else:

        query = query.order_by(Task.id.desc())

    tasks = query.all()

    statuses = task_status.ALL_STATUSES

    priorities = [
        "Low",
        "Medium",
        "High",
        "Urgent"
    ]

    board_columns = {
        status: []
        for status in task_status.BOARD_STATUSES
    }

    # Void is deliberately not a board column - cancelled work should
    # not sit in the flow competing for attention. Those tasks stay
    # reachable through the status filter instead.
    voided_tasks = []

    for task in tasks:
        if task.status == task_status.VOID:
            voided_tasks.append(task)
        else:
            board_columns.setdefault(task.status, []).append(task)

    # A voided task was cancelled by the client, so counting it either
    # way would misrepresent the team: it is neither delivered work nor
    # outstanding work. It is left out of every figure below.
    counted_tasks = [
        task for task in tasks
        if task.status not in task_status.EXCLUDED_FROM_METRICS
    ]

    total_tasks = len(counted_tasks)

    completed_tasks = len([
        task for task in counted_tasks
        if task.employee_completed
    ])

    review_tasks = len([
        task for task in counted_tasks
        if task.status in [
            task_status.CORE_REVIEW,
            task_status.CLIENT_REVIEW
        ]
    ])

    # An on-hold task is blocked by someone outside the team, so it is
    # not the assignee's fault that the deadline is passing - it does
    # not count as overdue while it is parked.
    overdue_tasks = len([
        task for task in counted_tasks
        if task.deadline
        and task.deadline < ist_now()
        and task.status in [
            task_status.ASSIGNED,
            task_status.IN_PROGRESS,
            task_status.PAUSED
        ]
    ])

    void_tasks = len(voided_tasks)

    task_ids = [task.id for task in tasks]

    file_counts = {}

    if task_ids:

        count_rows = (
            db.session.query(
                TaskFile.task_id,
                TaskFile.folder_type,
                db.func.count(TaskFile.id)
            )
            .filter(
                TaskFile.task_id.in_(task_ids),
                TaskFile.folder_type.in_(["reference", "submission"])
            )
            .group_by(TaskFile.task_id, TaskFile.folder_type)
            .all()
        )

        for row_task_id, row_folder_type, row_count in count_rows:
            file_counts.setdefault(
                row_task_id,
                {"reference": 0, "submission": 0}
            )
            file_counts[row_task_id][row_folder_type] = row_count

    return render_template(
        "tasks/list.html",
        tasks=tasks,
        board_columns=board_columns,
        # Board columns explain themselves via task_status.description().
        task_status=task_status,
        statuses=statuses,
        priorities=priorities,
        selected_status=selected_status,
        selected_priority=selected_priority,
        search=search,
        sort_by=sort_by,
        filter_by=filter_by,
        total_tasks=total_tasks,
        completed_tasks=completed_tasks,
        review_tasks=review_tasks,
        overdue_tasks=overdue_tasks,
        void_tasks=void_tasks,
        voided_tasks=voided_tasks,
        file_counts=file_counts
    )

@tasks_bp.route("/<int:task_id>/files-panel/<string:folder_type>")
@login_required
def task_files_panel(task_id, folder_type):

    if folder_type not in ("reference", "submission"):
        return jsonify(success=False, message="Invalid folder type."), 400

    task = Task.query.get_or_404(task_id)

    if not has_permission(current_user, "manage_tasks"):

        can_view = (
            task.assigned_to_id == current_user.id
            or current_user in task.visible_to
        )

        if not can_view:
            return jsonify(success=False, message="Not allowed."), 403

    files = (
        TaskFile.query
        .filter_by(task_id=task.id, folder_type=folder_type)
        .order_by(TaskFile.created_at.desc())
        .all()
    )

    file_list = []

    for task_file in files:
        file_list.append({
            "id": task_file.id,
            "filename": task_file.original_filename,
            "mime_type": task_file.mime_type or "",
            "is_image": bool(
                task_file.mime_type
                and task_file.mime_type.startswith("image/")
            ),
            "is_video": bool(
                task_file.mime_type
                and task_file.mime_type.startswith("video/")
            ),
            "preview_url": url_for(
                "tasks.preview_task_file",
                file_id=task_file.id
            ),
            # Tiles use this; preview_url is for actually opening the file.
            "thumb_url": url_for(
                "tasks.task_file_thumbnail",
                file_id=task_file.id
            ),
            "download_url": url_for(
                "tasks.download_task_file",
                file_id=task_file.id
            ),
        })

    return jsonify(success=True, files=file_list)


@tasks_bp.route("/filtered/<string:filter_type>")
@login_required
def filtered_tasks(filter_type):

    search = request.args.get("q", "").strip()

    query = get_task_base_query()

    page_title = "Tasks"
    page_subtitle = "Filtered task list"

    if filter_type in ["total", "all"]:

        page_title = "Total Tasks"
        page_subtitle = "All tasks available to you"

        # No extra filter required.
        # Base query already contains all tasks visible to current user.

    elif filter_type == "review":

        page_title = "In Review Tasks"
        page_subtitle = "Tasks currently in Core Review or Client Review"

        query = query.filter(
            Task.status.in_([
                "Core Review",
                "Client Review"
            ])
        )

    elif filter_type == "completed":

        page_title = "Completed Tasks"
        page_subtitle = "Tasks submitted by employees for review"

        query = query.filter(
            Task.employee_completed.is_(True)
        )

    elif filter_type == "overdue":

        page_title = "Overdue Tasks"
        page_subtitle = "Tasks whose deadline has passed"

        query = query.filter(
            Task.deadline.isnot(None),
            Task.deadline < ist_now(),
            Task.status.in_([
                "Assigned",
                "In Progress",
                "Paused"
            ])
        )

    else:

        flash(
            "Invalid task filter.",
            "error"
        )

        return redirect(
            url_for("tasks.list_tasks")
        )

    if search:
        query = apply_task_search(
            query,
            search
        )

    tasks = query.order_by(
        Task.deadline.asc().nullslast(),
        Task.id.desc()
    ).all()

    return render_template(
        "tasks/filtered.html",
        tasks=tasks,
        filter_type=filter_type,
        page_title=page_title,
        page_subtitle=page_subtitle,
        search=search,
        timedelta=timedelta
    )

@tasks_bp.route("/suggestions")
@login_required
def task_suggestions():

    search = request.args.get("q", "").strip()
    selected_status = request.args.get("status", "").strip()
    selected_priority = request.args.get("priority", "").strip()

    if len(search) < 1:
        return jsonify({
            "suggestions": []
        })

    query = get_task_base_query()

    if selected_status:
        query = query.filter(
            Task.status == selected_status
        )

    if selected_priority:
        query = query.filter(
            Task.priority == selected_priority
        )

    query = apply_task_search(
        query,
        search
    )

    tasks = query.order_by(
        Task.id.desc()
    ).limit(8).all()

    suggestions = []

    for task in tasks:

        suggestions.append({
            "id": task.id,
            "task_code": task.task_code,
            "title": task.title,
            "client": task.client.client_name if task.client else "-",
            "assigned_to": task.assigned_to.name if task.assigned_to else "Unassigned",
            "status": task.status
        })

    return jsonify({
        "suggestions": suggestions
    })


def in_panel():
    """True when this page is being rendered inside the task drawer.

    The drawer loads pages with ?panel=1. Both task forms post to their
    own URL (no action attribute), so the flag survives the POST - but a
    validation redirect built with url_for() would drop it and render
    the full app shell, sidebar and all, inside the drawer. Redirects
    that go back to a form therefore have to carry it along.
    """
    return request.args.get("panel") == "1"


def panel_args():
    """url_for() kwargs that keep the drawer flag on a redirect."""
    return {"panel": "1"} if in_panel() else {}


@tasks_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_task():

    # Rebuilt on every redirect back to this form so the drawer flag,
    # if there is one, survives the round trip.
    form_url = url_for("tasks.add_task", **panel_args())

    if not has_permission(current_user, "manage_tasks"):
        flash(
            (
                "You don't have permission to assign tasks. "
                "You can self assign your own task."
            ),
            "error",
        )

        return redirect(
            url_for("tasks.self_assign_task", **panel_args())
        )

    clients = (
        Client.query
        .filter_by(status="active")
        .order_by(Client.client_name.asc())
        .all()
    )

    deliverables = (
        ClientDeliverable.query
        .order_by(ClientDeliverable.id.desc())
        .all()
    )

    employees = (
        User.query
        .filter(
            User.status == "active",
            User.role.in_(
                [
                    "super_admin",
                    "admin",
                    "employee",
                ]
            ),
        )
        .order_by(User.name.asc())
        .all()
    )

    if request.method == "POST":

        uploaded_object_keys = []

        deadline = None
        deadline_value = request.form.get(
            "deadline",
            "",
        ).strip()

        if deadline_value:
            try:
                deadline = datetime.strptime(
                    deadline_value,
                    "%Y-%m-%dT%H:%M",
                )

            except ValueError:
                flash(
                    "Deadline format is invalid.",
                    "error",
                )

                return redirect(
                    form_url
                )

        try:
            client_id = int(
                request.form.get("client_id")
            )

            deliverable_id = int(
                request.form.get("deliverable_id")
            )

            assigned_to_id = int(
                request.form.get("assigned_to_id")
            )

        except (TypeError, ValueError):
            flash(
                (
                    "Please fill all required task "
                    "fields correctly."
                ),
                "error",
            )

            return redirect(
                form_url
            )

        try:
            quantity = float(
                request.form.get("quantity") or 1
            )

            estimated_time = float(
                request.form.get("estimated_time") or 1
            )

        except (TypeError, ValueError):
            flash(
                (
                    "Quantity and estimated time "
                    "must be valid numbers."
                ),
                "error",
            )

            return redirect(
                form_url
            )

        if quantity <= 0 or estimated_time <= 0:
            flash(
                (
                    "Quantity and estimated time "
                    "must be greater than zero."
                ),
                "error",
            )

            return redirect(
                form_url
            )

        title = request.form.get(
            "title",
            "",
        ).strip()

        if not title:
            flash(
                "Task title is required.",
                "error",
            )

            return redirect(
                form_url
            )

        deliverable = db.session.get(
            ClientDeliverable,
            deliverable_id,
        )

        if not deliverable:
            flash(
                "Invalid deliverable selected.",
                "error",
            )

            return redirect(
                form_url
            )

        if not deliverable.monthly_target:
            flash(
                (
                    "Selected deliverable has no "
                    "monthly target."
                ),
                "error",
            )

            return redirect(
                form_url
            )

        if (
            deliverable.monthly_target.client_id
            != client_id
        ):
            flash(
                (
                    "Selected deliverable does not "
                    "belong to selected client."
                ),
                "error",
            )

            return redirect(
                form_url
            )

        assigned_user = (
            User.query
            .filter_by(
                id=assigned_to_id,
                status="active",
            )
            .first()
        )

        if not assigned_user:
            flash(
                "Selected employee is invalid.",
                "error",
            )

            return redirect(
                form_url
            )

        reference_files = [
            uploaded_file
            for uploaded_file
            in request.files.getlist(
                "reference_files"
            )
            if (
                uploaded_file
                and (
                    uploaded_file.filename
                    or ""
                ).strip()
            )
        ]

        task = Task(
            title=title,
            description=request.form.get(
                "description",
                "",
            ).strip(),
            client_id=client_id,
            deliverable_id=deliverable_id,
            assigned_to_id=assigned_to_id,
            priority=request.form.get(
                "priority",
                "Medium",
            ),
            deadline=deadline,
            status="Assigned",
            quantity=quantity,
            estimated_time=estimated_time,
            status_started_at=datetime.utcnow(),
            created_by_id=current_user.id,
            task_code=generate_task_code(),
        )

        visibility_ids = request.form.getlist(
            "visibility_ids"
        )

        for user_id in visibility_ids:

            try:
                user_id = int(user_id)

            except (TypeError, ValueError):
                continue

            visible_user = (
                User.query
                .filter(
                    User.id == user_id,
                    User.status == "active",
                    User.role.in_(
                        [
                            "super_admin",
                            "admin",
                            "employee",
                        ]
                    ),
                )
                .first()
            )

            if (
                visible_user
                and visible_user not in task.visible_to
            ):
                task.visible_to.append(
                    visible_user
                )

        storage = None

        try:
            db.session.add(task)

            # Generates task.id before building the R2 object key.
            db.session.flush()

            storage = StorageService()

            for reference_file in reference_files:
                upload_result = (
                    storage.upload_task_file(
                        task=task,
                        file_storage=reference_file,
                        uploaded_by_id=current_user.id,
                        folder_type="reference",
                        is_final=False,

                    )
                )

                object_key = (
                    upload_result[
                        "provider_metadata"
                    ].get("object_key")
                )

                if object_key:
                    uploaded_object_keys.append(
                        object_key
                    )

            add_activity(
                task,
                action="created",
                message=(
                    f"Created by {current_user.name}"
                ),
                old_status=None,
                new_status="Assigned",
            )

            create_notification(
                user_id=assigned_to_id,
                title="New task assigned",
                message=(
                    f"{current_user.name} assigned "
                    f"you: {task.title}"
                ),
                link=url_for(
                    "tasks.task_detail",
                    task_id=task.id,
                ),
                actor_id=current_user.id,
                task_id=task.id,
            )

            for visible_user in task.visible_to:

                if visible_user.id == assigned_to_id:
                    continue

                create_notification(
                    user_id=visible_user.id,
                    title="Task shared with you",
                    message=(
                        f"{current_user.name} shared: "
                        f"{task.title}"
                    ),
                    link=url_for(
                        "tasks.task_detail",
                        task_id=task.id,
                    ),
                    actor_id=current_user.id,
                    task_id=task.id,
                )

            db.session.commit()

        except StorageServiceError as error:
            db.session.rollback()

            if storage is not None:
                for object_key in uploaded_object_keys:
                    try:
                        storage.delete(
                            object_key=object_key
                        )

                    except Exception:
                        current_app.logger.exception(
                            (
                                "Unable to clean up R2 "
                                "object after failed task "
                                "creation: %s"
                            ),
                            object_key,
                        )

            current_app.logger.exception(
                "Reference file upload failed."
            )

            flash(
                (
                    "Task could not be created because "
                    "a reference file upload failed. "
                    f"{error}"
                ),
                "error",
            )

            return redirect(
                form_url
            )

        except Exception:
            db.session.rollback()

            if storage is not None:
                for object_key in uploaded_object_keys:
                    try:
                        storage.delete(
                            object_key=object_key
                        )

                    except Exception:
                        current_app.logger.exception(
                            (
                                "Unable to clean up R2 "
                                "object after failed task "
                                "creation: %s"
                            ),
                            object_key,
                        )

            current_app.logger.exception(
                "Unexpected task creation failure."
            )

            flash(
                (
                    "Task could not be created due to "
                    "an unexpected error."
                ),
                "error",
            )

            return redirect(
                form_url
            )

        flash(
            "Task created successfully.",
            "success",
        )

        return redirect(
            url_for("tasks.list_tasks")
        )

    deadline_default = request.args.get(
        "deadline",
        "",
    )

    return render_template(
        "tasks/add.html",
        panel_mode=in_panel(),
        clients=clients,
        deliverables=deliverables,
        employees=employees,
        deadline_default=deadline_default,
    )

@tasks_bp.route("/self-assign", methods=["GET", "POST"])
@login_required
def self_assign_task():

    # See add_task: keeps the drawer flag across validation redirects.
    form_url = url_for("tasks.self_assign_task", **panel_args())

    clients = Client.query.filter_by(
        status="active"
    ).order_by(
        Client.client_name.asc()
    ).all()

    deliverables = ClientDeliverable.query.order_by(
        ClientDeliverable.id.desc()
    ).all()

    if request.method == "POST":

        uploaded_object_keys = []

        deadline = None
        deadline_value = request.form.get("deadline")

        if deadline_value:
            deadline = datetime.strptime(
                deadline_value,
                "%Y-%m-%dT%H:%M"
            )

        try:
            client_id = int(request.form.get("client_id"))
            deliverable_id = int(request.form.get("deliverable_id"))

        except (TypeError, ValueError):
            flash("Please select client and deliverable.", "error")
            return redirect(form_url)

        try:
            quantity = float(request.form.get("quantity") or 1)
            estimated_time = float(request.form.get("estimated_time") or 1)

        except (TypeError, ValueError):
            flash("Quantity and estimated time must be valid.", "error")
            return redirect(form_url)

        if quantity <= 0 or estimated_time <= 0:
            flash("Quantity and estimated time must be greater than zero.", "error")
            return redirect(form_url)

        deliverable = ClientDeliverable.query.get(deliverable_id)

        if not deliverable or not deliverable.monthly_target:
            flash("Invalid deliverable selected.", "error")
            return redirect(form_url)

        if deliverable.monthly_target.client_id != client_id:
            flash("Selected deliverable does not belong to selected client.", "error")
            return redirect(form_url)

        title = request.form.get("title", "").strip()

        if not title:
            flash("Task title is required.", "error")
            return redirect(form_url)

        reference_files = [
            uploaded_file
            for uploaded_file
            in request.files.getlist("reference_files")
            if (
                uploaded_file
                and (uploaded_file.filename or "").strip()
            )
        ]

        task = Task(
            title=title,
            description=request.form.get("description", "").strip(),
            client_id=client_id,
            deliverable_id=deliverable_id,
            assigned_to_id=current_user.id,
            priority=request.form.get("priority"),
            deadline=deadline,
            status="Assigned",
            quantity=quantity,
            estimated_time=estimated_time,
            status_started_at=datetime.utcnow(),
            created_by_id=current_user.id,
            task_code=generate_task_code()
        )

        storage = None

        try:
            db.session.add(task)

            # Generates task.id before building the R2 object key.
            db.session.flush()

            storage = StorageService()

            for reference_file in reference_files:
                upload_result = storage.upload_task_file(
                    task=task,
                    file_storage=reference_file,
                    uploaded_by_id=current_user.id,
                    folder_type="reference",
                    is_final=False,
                )

                object_key = (
                    upload_result["provider_metadata"].get("object_key")
                )

                if object_key:
                    uploaded_object_keys.append(object_key)

            add_activity(
                task,
                action="created",
                message=f"Self assigned by {current_user.name}",
                old_status=None,
                new_status="Assigned"
            )

            db.session.commit()

        except StorageServiceError as error:
            db.session.rollback()

            if storage is not None:
                for object_key in uploaded_object_keys:
                    try:
                        storage.delete(object_key=object_key)
                    except Exception:
                        current_app.logger.exception(
                            "Unable to clean up R2 object after failed self assign: %s",
                            object_key,
                        )

            current_app.logger.exception(
                "Reference file upload failed during self assign."
            )

            flash(
                f"Task could not be created because a reference file upload failed. {error}",
                "error",
            )

            return redirect(form_url)

        except Exception:
            db.session.rollback()

            if storage is not None:
                for object_key in uploaded_object_keys:
                    try:
                        storage.delete(object_key=object_key)
                    except Exception:
                        current_app.logger.exception(
                            "Unable to clean up R2 object after failed self assign: %s",
                            object_key,
                        )

            current_app.logger.exception(
                "Unexpected self assign task creation failure."
            )

            flash(
                "Task could not be created due to an unexpected error.",
                "error",
            )

            return redirect(form_url)

        flash("Task self assigned successfully.", "success")

        return redirect(
            url_for(
                "tasks.task_detail",
                task_id=task.id
            )
        )

    deadline_default = request.args.get("deadline", "")

    return render_template(
        "tasks/self_assign.html",
        panel_mode=in_panel(),
        clients=clients,
        deliverables=deliverables,
        deadline_default=deadline_default
    )

@tasks_bp.route("/<int:task_id>/edit", methods=["GET", "POST"])
@login_required
def edit_task(task_id):

    task = Task.query.get_or_404(task_id)

    is_self_assigned_owner = (
        task.created_by_id == current_user.id
        and task.assigned_to_id == current_user.id
    )

    if not has_permission(current_user, "manage_tasks") and not is_self_assigned_owner:
        return redirect(url_for("dashboard.index"))

    clients = Client.query.filter_by(
        status="active"
    ).order_by(
        Client.client_name.asc()
    ).all()

    deliverables = ClientDeliverable.query.order_by(
        ClientDeliverable.id.desc()
    ).all()

    employees = User.query.filter(
        User.status == "active",
        User.role.in_(["super_admin", "admin", "employee"])
    ).order_by(
        User.name.asc()
    ).all()

    if request.method == "POST":

        old_status = task.status
        old_assigned_to_id = task.assigned_to_id

        old_title = task.title
        old_description = task.description or ""
        old_client = task.client.client_name if task.client else "-"
        old_deliverable = task.deliverable.deliverable_name if task.deliverable else "-"
        old_assigned_to = task.assigned_to.name if task.assigned_to else "-"
        old_priority = task.priority
        old_deadline = (
            task.deadline.strftime("%d %b %Y %I:%M %p")
            if task.deadline else "-"
        )
        old_quantity = task.quantity or 1
        old_estimated_time = task.estimated_time or 1
        old_visibility_names = sorted(
            [user.name for user in task.visible_to]
        )

        changes = []

        deadline = None

        if request.form.get("deadline"):
            deadline = datetime.strptime(
                request.form.get("deadline"),
                "%Y-%m-%dT%H:%M"
            )

        try:
            client_id = int(request.form.get("client_id"))
            deliverable_id = int(request.form.get("deliverable_id"))
            assigned_to_id = int(request.form.get("assigned_to_id"))
            quantity = float(request.form.get("quantity") or 1)
            estimated_time = float(request.form.get("estimated_time") or 1)

        except (TypeError, ValueError):
            flash(
                "Please fill all required task fields correctly.",
                "error"
            )
            return redirect(
                url_for(
                    "tasks.edit_task",
                    task_id=task.id
                )
            )

        if quantity <= 0 or estimated_time <= 0:
            flash(
                "Quantity and estimated time must be greater than zero.",
                "error"
            )
            return redirect(
                url_for(
                    "tasks.edit_task",
                    task_id=task.id
                )
            )

        deliverable = ClientDeliverable.query.get(deliverable_id)

        if not deliverable:
            flash(
                "Invalid deliverable selected.",
                "error"
            )
            return redirect(
                url_for(
                    "tasks.edit_task",
                    task_id=task.id
                )
            )

        if not deliverable.monthly_target:
            flash(
                "Selected deliverable has no monthly target.",
                "error"
            )
            return redirect(
                url_for(
                    "tasks.edit_task",
                    task_id=task.id
                )
            )

        if deliverable.monthly_target.client_id != client_id:
            flash(
                "Selected deliverable does not belong to selected client.",
                "error"
            )
            return redirect(
                url_for(
                    "tasks.edit_task",
                    task_id=task.id
                )
            )

        assigned_user = User.query.filter_by(
            id=assigned_to_id,
            status="active"
        ).first()

        if not assigned_user:
            flash(
                "Selected employee is invalid.",
                "error"
            )
            return redirect(
                url_for(
                    "tasks.edit_task",
                    task_id=task.id
                )
            )

        new_title = request.form.get("title", "").strip()
        new_description = request.form.get("description", "").strip()
        new_priority = request.form.get("priority")
        new_status = request.form.get("status")

        # Void and On Hold are set from the task page, where a reason
        # can be captured, so they are not offered in this dropdown.
        allowed_statuses = task_status.SELECTABLE_STATUSES

        if new_status not in allowed_statuses:
            flash(
                "Invalid task status selected.",
                "error"
            )
            return redirect(
                url_for(
                    "tasks.edit_task",
                    task_id=task.id
                )
            )

        if not new_title:
            flash(
                "Task title is required.",
                "error"
            )
            return redirect(
                url_for(
                    "tasks.edit_task",
                    task_id=task.id
                )
            )

        task.title = new_title
        task.description = new_description
        task.client_id = client_id
        task.deliverable_id = deliverable_id
        task.assigned_to_id = assigned_to_id
        task.priority = new_priority
        task.deadline = deadline
        task.quantity = quantity
        task.estimated_time = estimated_time

        new_client = Client.query.get(client_id)
        new_client_name = new_client.client_name if new_client else "-"

        new_deliverable_name = deliverable.deliverable_name
        new_assigned_to = assigned_user.name

        new_deadline = (
            task.deadline.strftime("%d %b %Y %I:%M %p")
            if task.deadline else "-"
        )

        if old_title != task.title:
            changes.append(
                ("Title", old_title, task.title)
            )

        if old_description != task.description:
            changes.append(
                ("Description", "Updated", "Updated")
            )

        if old_client != new_client_name:
            changes.append(
                ("Client", old_client, new_client_name)
            )

        if old_deliverable != new_deliverable_name:
            changes.append(
                ("Deliverable", old_deliverable, new_deliverable_name)
            )

        if old_assigned_to != new_assigned_to:
            changes.append(
                ("Assigned To", old_assigned_to, new_assigned_to)
            )

        if old_priority != task.priority:
            changes.append(
                ("Priority", old_priority, task.priority)
            )

        if old_deadline != new_deadline:
            changes.append(
                ("Deadline", old_deadline, new_deadline)
            )

        if float(old_quantity) != float(task.quantity):
            changes.append(
                ("Quantity", old_quantity, task.quantity)
            )

        if float(old_estimated_time) != float(task.estimated_time):
            changes.append(
                (
                    "Estimated Time / Qty",
                    old_estimated_time,
                    task.estimated_time
                )
            )

        if new_status != task.status:
            changes.append(
                ("Status", task.status, new_status)
            )

            if task.timer_started_at and new_status != "In Progress":
                pause_timer(task)

            if new_status == "Published":
                pause_timer(task)
                task.completed_at = ist_now()

            record_status_time(
                task,
                new_status
            )

            if new_status in ["Core Review", "Client Review", "Published"]:
                task.employee_completed = True

                if not task.employee_completed_at:
                    task.employee_completed_at = ist_now()

        task.visible_to.clear()

        visibility_ids = request.form.getlist("visibility_ids")

        for user_id in visibility_ids:

            try:
                user_id = int(user_id)

            except (TypeError, ValueError):
                continue

            user = User.query.filter(
                User.id == user_id,
                User.status == "active",
                User.role.in_(["super_admin", "admin", "employee"])
            ).first()

            if user and user not in task.visible_to:
                task.visible_to.append(user)

        new_visibility_names = sorted(
            [user.name for user in task.visible_to]
        )

        if old_visibility_names != new_visibility_names:
            changes.append(
                (
                    "Visibility",
                    ", ".join(old_visibility_names) or "-",
                    ", ".join(new_visibility_names) or "-"
                )
            )

        add_activity(
            task,
            action="updated",
            message=build_task_update_message(changes),
            old_status=old_status,
            new_status=task.status
        )

        if old_assigned_to_id != task.assigned_to_id:
            task.employee_completed = False
            task.employee_completed_at = None
            create_notification(
                user_id=task.assigned_to_id,
                title="Task assigned to you",
                message=f"{current_user.name} assigned you: {task.title}",
                link=url_for("tasks.task_detail", task_id=task.id),
                actor_id=current_user.id,
                task_id=task.id
            )

            if old_assigned_to_id:
                create_notification(
                    user_id=old_assigned_to_id,
                    title="Task reassigned",
                    message=f"{task.title} is no longer assigned to you.",
                    link=url_for("tasks.task_detail", task_id=task.id),
                    actor_id=current_user.id,
                    task_id=task.id
                )

        else:
            create_notification(
                user_id=task.assigned_to_id,
                title="Task Updated",
                message=f"{current_user.name} updated: {task.title}",
                link=url_for("tasks.task_detail", task_id=task.id),
                actor_id=current_user.id,
                task_id=task.id
            )

        for user in task.visible_to:
            if user.id != task.assigned_to_id:
                create_notification(
                    user_id=user.id,
                    title="Task Updated",
                    message=f"{current_user.name} updated shared task: {task.title}",
                    link=url_for("tasks.task_detail", task_id=task.id),
                    actor_id=current_user.id,
                    task_id=task.id
                )

        db.session.commit()

        flash(
            "Task updated successfully.",
            "success"
        )

        return redirect(
            url_for(
                "tasks.task_detail",
                task_id=task.id
            )
        )

    return render_template(
        "tasks/edit.html",
        task=task,
        clients=clients,
        deliverables=deliverables,
        employees=employees,
        task_status=task_status
    )

@tasks_bp.route("/<int:task_id>/start", methods=["POST"])
@login_required
def start_task(task_id):

    task = Task.query.get_or_404(task_id)
    can_manage = has_permission(current_user, "manage_tasks")

    if not can_manage and task.assigned_to_id != current_user.id:
        flash(
            "You are not allowed to update this task.",
            "error"
        )
        return redirect(url_for("tasks.list_tasks"))

    running_task = None

    if not has_permission(current_user, "manage_tasks"):

        running_task = Task.query.filter(
            Task.assigned_to_id == task.assigned_to_id,
            Task.id != task.id,
            Task.timer_started_at.isnot(None),
            Task.status == "In Progress"
        ).first()
    running_task = Task.query.filter(
        Task.assigned_to_id == task.assigned_to_id,
        Task.id != task.id,
        Task.timer_started_at.isnot(None),
        Task.status == "In Progress"
    ).first()

    if running_task:

        pause_timer(running_task)

        add_activity(
            running_task,
            action="auto_paused",
            message=f"Auto paused because {current_user.name} started another task: {task.title}",
            old_status="In Progress",
            new_status="In Progress"
        )

    if task.status == "Assigned":

        old_status = record_status_time(
            task,
            "In Progress"
        )
        task.employee_completed = False
        task.employee_completed_at = None

        add_activity(
            task,
            action="started",
            message=f"Started by {current_user.name}",
            old_status=old_status,
            new_status="In Progress"
        )

    elif task.status == "Paused":
        old_status = record_status_time(
            task,
            "In Progress"
        )

        add_activity(
            task,
            action="resumed",
            message=f"Resumed from Paused by {current_user.name}",
            old_status=old_status,
            new_status="In Progress"
        )

    elif task.status == "In Progress" and not task.timer_started_at:

        add_activity(
            task,
            action="resumed",
            message=f"Resumed by {current_user.name}",
            old_status="In Progress",
            new_status="In Progress"
        )

    if task.status == "In Progress":

        start_timer(task)
        flash(
            "Task timer started.",
            "success"
        )

    else:

        flash(
    "Only Assigned, Paused or in-progress tasks can be started.",
    "error"
)

    db.session.commit()

    return redirect(
        request.referrer or url_for(
            "tasks.task_detail",
            task_id=task.id
        )
    )


@tasks_bp.route("/<int:task_id>/pause", methods=["POST"])
@login_required
def pause_task(task_id):

    task = Task.query.get_or_404(task_id)

    if (
        task.assigned_to_id != current_user.id
        and
        not has_permission(current_user, "manage_tasks")
    ):
        flash(
            "You are not allowed to pause this task.",
            "error"
        )
        return redirect(url_for("tasks.list_tasks"))

    pause_timer(task)

    old_status = record_status_time(
        task,
        "Paused"
    )

    add_activity(
        task,
        action="paused",
        message=f"Put on Paused by {current_user.name}",
        old_status=old_status,
        new_status="Paused"
    )

    db.session.commit()

    flash(
        "Task paused.",
        "success"
    )

    return redirect(
        request.referrer or url_for(
            "tasks.task_detail",
            task_id=task.id
        )
    )


@tasks_bp.route("/<int:task_id>/hold", methods=["POST"])
@login_required
def hold_task(task_id):
    """Park a task that is blocked by something outside the team."""

    task = Task.query.get_or_404(task_id)

    # Unlike Paused, this is not the assignee's call: a task goes on
    # hold because a client or another external party is blocking it,
    # and only a manager can judge when that block has cleared.
    if not has_permission(current_user, "manage_tasks"):
        flash(
            "Only a manager can put a task on hold.",
            "error"
        )
        return redirect(
            url_for("tasks.task_detail", task_id=task.id)
        )

    if not task_status.can_move(
        task.status, task_status.ON_HOLD, True
    ):
        flash(
            f"A task in {task.status} cannot be put on hold.",
            "error"
        )
        return redirect(
            url_for("tasks.task_detail", task_id=task.id)
        )

    reason = request.form.get("reason", "").strip()

    if len(reason) < 10:
        flash(
            "Please give a reason of at least 10 characters "
            "so the team knows what this task is waiting on.",
            "error"
        )
        return redirect(
            url_for("tasks.task_detail", task_id=task.id)
        )

    pause_timer(task)

    task.hold_reason = reason
    task.held_at = datetime.utcnow()
    task.held_by_id = current_user.id

    old_status = record_status_time(task, task_status.ON_HOLD)

    add_activity(
        task,
        action="held",
        message=f"Put On Hold by {current_user.name}: {reason}",
        old_status=old_status,
        new_status=task_status.ON_HOLD
    )

    if task.assigned_to_id and task.assigned_to_id != current_user.id:
        create_notification(
            user_id=task.assigned_to_id,
            title="Task put on hold",
            message=(
                f"{current_user.name} put "
                f"{task.title} on hold: {reason}"
            ),
            link=url_for("tasks.task_detail", task_id=task.id),
            actor_id=current_user.id,
            task_id=task.id
        )

    db.session.commit()

    flash("Task put on hold.", "success")

    return redirect(
        request.referrer
        or url_for("tasks.task_detail", task_id=task.id)
    )


@tasks_bp.route("/<int:task_id>/resume", methods=["POST"])
@login_required
def resume_task(task_id):
    """Bring a task back from On Hold once the blocker has cleared."""

    task = Task.query.get_or_404(task_id)

    if not has_permission(current_user, "manage_tasks"):
        flash(
            "Only a manager can take a task off hold.",
            "error"
        )
        return redirect(
            url_for("tasks.task_detail", task_id=task.id)
        )

    if task.status != task_status.ON_HOLD:
        flash("This task is not on hold.", "error")
        return redirect(
            url_for("tasks.task_detail", task_id=task.id)
        )

    # Back to Assigned rather than straight into In Progress: the
    # assignee decides when to actually pick the work back up, and
    # starting their timer for them would be wrong.
    old_status = record_status_time(task, task_status.ASSIGNED)

    task.hold_reason = None
    task.held_at = None
    task.held_by_id = None

    add_activity(
        task,
        action="resumed",
        message=f"Taken off hold by {current_user.name}",
        old_status=old_status,
        new_status=task_status.ASSIGNED
    )

    if task.assigned_to_id and task.assigned_to_id != current_user.id:
        create_notification(
            user_id=task.assigned_to_id,
            title="Task off hold",
            message=(
                f"{task.title} is off hold "
                "and ready to pick up again."
            ),
            link=url_for("tasks.task_detail", task_id=task.id),
            actor_id=current_user.id,
            task_id=task.id
        )

    db.session.commit()

    flash("Task taken off hold.", "success")

    return redirect(
        request.referrer
        or url_for("tasks.task_detail", task_id=task.id)
    )


@tasks_bp.route("/<int:task_id>/void", methods=["POST"])
@login_required
def void_task(task_id):
    """Close a task the client cancelled part-way through."""

    task = Task.query.get_or_404(task_id)

    if not has_permission(current_user, "manage_tasks"):
        flash(
            "Only a manager can void a task.",
            "error"
        )
        return redirect(
            url_for("tasks.task_detail", task_id=task.id)
        )

    if not task_status.can_move(
        task.status, task_status.VOID, True
    ):
        flash(
            f"A task in {task.status} cannot be voided.",
            "error"
        )
        return redirect(
            url_for("tasks.task_detail", task_id=task.id)
        )

    reason = request.form.get("reason", "").strip()

    if len(reason) < 10:
        flash(
            "Please record why this task was cancelled - a voided "
            "task with no reason is impossible to audit later.",
            "error"
        )
        return redirect(
            url_for("tasks.task_detail", task_id=task.id)
        )

    pause_timer(task)

    task.void_reason = reason
    task.voided_at = datetime.utcnow()
    task.voided_by_id = current_user.id

    old_status = record_status_time(task, task_status.VOID)

    add_activity(
        task,
        action="voided",
        message=f"Voided by {current_user.name}: {reason}",
        old_status=old_status,
        new_status=task_status.VOID
    )

    if task.assigned_to_id and task.assigned_to_id != current_user.id:
        create_notification(
            user_id=task.assigned_to_id,
            title="Task voided",
            message=(
                f"{current_user.name} voided "
                f"{task.title}: {reason}"
            ),
            link=url_for("tasks.task_detail", task_id=task.id),
            actor_id=current_user.id,
            task_id=task.id
        )

    db.session.commit()

    flash(
        "Task voided. It is excluded from performance figures.",
        "success"
    )

    return redirect(
        request.referrer
        or url_for("tasks.task_detail", task_id=task.id)
    )


@tasks_bp.route("/<int:task_id>/restore", methods=["POST"])
@login_required
def restore_task(task_id):
    """Undo a void. Rare, but voiding by mistake must be recoverable."""

    task = Task.query.get_or_404(task_id)

    if not has_permission(current_user, "manage_tasks"):
        flash(
            "Only a manager can restore a voided task.",
            "error"
        )
        return redirect(
            url_for("tasks.task_detail", task_id=task.id)
        )

    if task.status != task_status.VOID:
        flash("This task is not voided.", "error")
        return redirect(
            url_for("tasks.task_detail", task_id=task.id)
        )

    old_status = record_status_time(task, task_status.ASSIGNED)

    task.void_reason = None
    task.voided_at = None
    task.voided_by_id = None

    add_activity(
        task,
        action="restored",
        message=f"Void reversed by {current_user.name}",
        old_status=old_status,
        new_status=task_status.ASSIGNED
    )

    db.session.commit()

    flash("Task restored to Assigned.", "success")

    return redirect(
        request.referrer
        or url_for("tasks.task_detail", task_id=task.id)
    )


@tasks_bp.route("/<int:task_id>/submit-review", methods=["POST"])
@login_required
def submit_review(task_id):

    task = Task.query.get_or_404(task_id)

    if task.assigned_to_id != current_user.id:
        flash(
            "You are not assigned to this task.",
            "error"
        )
        return redirect(url_for("tasks.list_tasks"))

    if task.status in ["Assigned", "In Progress"]:

        pause_timer(task)

        if not task.employee_completed:
            task.employee_completed = True
            task.employee_completed_at = ist_now()

        old_status = record_status_time(
            task,
            "Core Review"
        )

        add_activity(
            task,
            action="submitted_review",
            message=f"Submitted for Core Review by {current_user.name}",
            old_status=old_status,
            new_status="Core Review"
        )

        reviewers = [
            user for user in User.query.filter_by(status="active").all()
            if has_permission(user, "approve_tasks")
        ]

        for reviewer in reviewers:
            create_notification(
                user_id=reviewer.id,
                title="Review requested",
                message=f"{current_user.name} submitted: {task.title}",
                link=url_for("tasks.task_detail", task_id=task.id),
                actor_id=current_user.id,
                task_id=task.id
            )

        db.session.commit()

        flash(
            "Task submitted for core review.",
            "success"
        )

    return redirect(url_for("tasks.list_tasks"))

@tasks_bp.route("/kanban/update-status", methods=["POST"])
@login_required
def kanban_update_status():

    data = request.get_json()

    if not data:
        return jsonify(
            {
                "success": False,
                "message": "Invalid request."
            }
        ), 400

    task_id = data.get("task_id")
    new_status = data.get("status")

    task = Task.query.get_or_404(task_id)
        # -------------------------------------------------
    # Employee can pull back their own task from
    # Core Review if it was submitted by mistake.
    # -------------------------------------------------

    if (
        task.assigned_to_id != current_user.id
        and
        not has_permission(current_user, "manage_tasks")
    ):
        return jsonify(
            {
                "success": False,
                "message": "Permission denied."
            }
        ), 403

    if new_status not in task_status.ALL_STATUSES:

        return jsonify(
            {
                "success": False,
                "message": "Invalid status."
            }
        ), 400

    can_manage = has_permission(current_user, "manage_tasks")

    # ---------------------------------------
    # Drag rules
    # ---------------------------------------

    if not task_status.can_move(task.status, new_status, can_manage):

        return jsonify({
            "success": False,
            "message": (
                "You cannot move this task to that status."
            )
        }), 403

    # Both need a written reason and the board has nowhere to type
    # one, so a drag can never produce either status - they are set
    # from the task page. Checking an existing reason would not do:
    # the reason has to describe this hold, not a previous one.
    if new_status in task_status.REASON_REQUIRED_STATUSES \
            or new_status == task_status.ON_HOLD:

        verb = (
            "void it"
            if new_status == task_status.VOID
            else "put it on hold"
        )

        return jsonify({
            "success": False,
            "message": (
                f"Open the task to {verb} - a reason is required."
            )
        }), 400

    old_status = task.status
    previous_task = None

    # --------------------------------------------
    # Only one In Progress task allowed
    # --------------------------------------------

    if (
        not has_permission(current_user, "manage_tasks")
        and new_status == "In Progress"
        and task.assigned_to_id
    ):

        previous_task = (
            Task.query.filter(
                Task.assigned_to_id == task.assigned_to_id,
                Task.status == "In Progress",
                Task.id != task.id
            ).first()
        )

        if previous_task:

            pause_timer(previous_task)

            record_status_time(
                previous_task,
                "Paused"
            )

            add_activity(
                previous_task,
                action="auto_paused",
                message=(
                    f"{previous_task.title} was automatically paused "
                    "because another task was started."
                ),
                old_status="In Progress",
                new_status="Paused"
            )

    # --------------------------------------------
    # Update current task status
    # --------------------------------------------

    record_status_time(
        task,
        new_status
    )
        # ---------------------------------------
# Timer automation
# ---------------------------------------

    current_time = datetime.utcnow()

    if new_status == "In Progress":

        if task.timer_started_at is None:
            task.timer_started_at = current_time

    elif new_status == "Paused":

        if task.timer_started_at:

            worked = (
                current_time -
                task.timer_started_at
            ).total_seconds()

            task.worked_seconds = (
                task.worked_seconds or 0
            ) + int(worked)

            task.timer_started_at = None

    elif new_status in [
        "Core Review",
        "Client Review",
        "Published",
    ]:

        if task.timer_started_at:

            worked = (
                current_time -
                task.timer_started_at
            ).total_seconds()

            task.worked_seconds = (
                task.worked_seconds or 0
            ) + int(worked)

            task.timer_started_at = None

    add_activity(
        task,
        action="status_changed",
        message=f"{current_user.name} moved task from {old_status} to {new_status}.",
    )

    db.session.commit()

    return jsonify(
        {
            "success": True,
            "message": (
                "Previous task was paused automatically."
                if (
                    new_status == "In Progress"
                    and task.assigned_to_id
                    and previous_task
                )
                else "Task updated successfully."
            )
        }
    )
@tasks_bp.route("/<int:task_id>/approve", methods=["POST"])
@login_required
def approve_task(task_id):

    if not has_permission(current_user, "approve_tasks"):
        return redirect(url_for("dashboard.index"))

    task = Task.query.get_or_404(task_id)
    status_changed = False

    if task.status in ["Core Review", "Client Review", "Published"]:
        task.employee_completed = True

        if not task.employee_completed_at:
            task.employee_completed_at = ist_now()

    if task.status == "Core Review":

        old_status = record_status_time(
            task,
            "Client Review"
        )

        add_activity(
            task,
            action="approved_core_review",
            message=f"Core Review approved by {current_user.name}",
            old_status=old_status,
            new_status="Client Review"
        )

        status_changed = True

        flash(
            "Task moved to client review.",
            "success"
        )

    elif task.status == "Client Review":

        if not task.deliverable:
            flash(
                "Task deliverable not found.",
                "error"
            )
            return redirect(
                request.referrer or url_for("tasks.list_tasks")
            )

        old_status = record_status_time(
            task,
            "Published"
        )

        add_activity(
            task,
            action="published",
            message=f"Published by {current_user.name}",
            old_status=old_status,
            new_status="Published"
        )

        task.completed_at = ist_now()
        status_changed = True

        task.deliverable.completed_count += 1

        flash(
            "Task published successfully.",
            "success"
        )

    else:

        flash(
            "This task is not ready for approval.",
            "error"
        )

    if status_changed:
        create_notification(
            user_id=task.assigned_to_id,
            title="Task status updated",
            message=f"{current_user.name} moved {task.title} to {task.status}",
            link=url_for("tasks.task_detail", task_id=task.id),
            actor_id=current_user.id,
            task_id=task.id
        )

    db.session.commit()

    return redirect(
        request.referrer or url_for("tasks.list_tasks")
    )


# Matches the reference_file input's accept="..." attribute in
# tasks/detail.html. This upload is saved straight to local disk and
# served back through Flask's static handler, which infers
# Content-Type from the file extension - so an unchecked upload named
# e.g. "x.html" or "x.svg" would be served as live, executable HTML
# from the app's own origin the moment anyone opened the reference
# file link (same-origin stored XSS, not just an isolated R2 domain).
REJECTION_FILE_ALLOWED_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "bmp",
    "mp4", "webm", "mov", "avi", "mkv",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
}


@tasks_bp.route("/<int:task_id>/reject", methods=["POST"])
@login_required
def reject_task(task_id):

    if not has_permission(current_user, "approve_tasks"):
        return redirect(url_for("dashboard.index"))

    task = Task.query.get_or_404(task_id)

    if task.status not in ["Core Review", "Client Review"]:
        flash(
            "This task cannot be rejected now.",
            "error"
        )
        return redirect(
            request.referrer or url_for(
                "tasks.task_detail",
                task_id=task.id
            )
        )

    message = request.form.get(
        "message",
        ""
    ).strip()

    reference_file = request.files.get(
        "reference_file"
    )

    if not message:
        flash(
            "Rejection reason is required.",
            "error"
        )
        return redirect(
            request.referrer or url_for(
                "tasks.task_detail",
                task_id=task.id
            )
        )

    file_name = None
    file_path = None
    file_type = None

    if reference_file and reference_file.filename:

        upload_folder = os.path.join(
            "app",
            "static",
            "uploads",
            "task_feedbacks"
        )

        os.makedirs(
            upload_folder,
            exist_ok=True
        )

        safe_name = secure_filename(
            reference_file.filename
        )

        file_extension = (
            safe_name.rsplit(".", 1)[-1].lower()
            if "." in safe_name else ""
        )

        if file_extension not in REJECTION_FILE_ALLOWED_EXTENSIONS:
            flash(
                "Reference file type not allowed. Please upload an "
                "image, video, PDF, Word, Excel or PowerPoint file.",
                "error"
            )
            return redirect(
                request.referrer or url_for(
                    "tasks.task_detail",
                    task_id=task.id
                )
            )

        file_name = f"task_{task.id}_{safe_name}"

        save_path = os.path.join(
            upload_folder,
            file_name
        )

        reference_file.save(save_path)

        file_path = f"uploads/task_feedbacks/{file_name}"
        file_type = reference_file.content_type

    feedback = TaskFeedback(
        task_id=task.id,
        sender_id=current_user.id,
        receiver_id=task.assigned_to_id,
        message=message,
        file_name=file_name,
        file_path=file_path,
        file_type=file_type
    )

    old_status = record_status_time(
        task,
        "Assigned"
    )

    task.employee_completed = False
    task.employee_completed_at = None
    task.timer_started_at = None
    task.started_at = None

    add_activity(
        task,
        action="rejected",
        message=f"Rejected by {current_user.name}: {message}",
        old_status=old_status,
        new_status="Assigned"
    )

    db.session.add(feedback)

    create_notification(
        user_id=task.assigned_to_id,
        title="Revision required",
        message=message,
        link=url_for("tasks.task_detail", task_id=task.id),
        actor_id=current_user.id,
        task_id=task.id
    )

    db.session.commit()

    flash(
        "Task rejected and moved back to assigned.",
        "success"
    )

    return redirect(
        request.referrer or url_for(
            "tasks.task_detail",
            task_id=task.id
        )
    )


@tasks_bp.route("/<int:task_id>")
@login_required
def task_detail(task_id):

    task = Task.query.get_or_404(task_id)
    reference_files = (
        TaskFile.query
        .filter_by(
            task_id=task.id,
            folder_type="reference",
        )
        .order_by(
            TaskFile.created_at.desc()
        )
        .all()
    )
    submission_files = (
        TaskFile.query
        .filter_by(
            task_id=task.id,
            folder_type="submission"
        )
        .order_by(
            TaskFile.created_at.desc()
        )
        .all()
    )

    working_files = (
        TaskFile.query
        .filter_by(
            task_id=task.id,
            folder_type="working",
        )
        .order_by(
            TaskFile.created_at.desc()
        )
        .all()
    )

    final_files = (
        TaskFile.query
        .filter_by(
            task_id=task.id,
            folder_type="final",
        )
        .order_by(
            TaskFile.created_at.desc()
        )
        .all()
    )

    if not has_permission(current_user, "manage_tasks"):

        can_view = (
            task.assigned_to_id == current_user.id
            or current_user in task.visible_to
        )

        if not can_view:
            return redirect(url_for("tasks.list_tasks"))

    live_seconds = get_live_worked_seconds(task)
    current_status_seconds = 0

    if task.status_started_at and task.status != "Published":
        current_status_seconds = int(
            (datetime.utcnow() - task.status_started_at).total_seconds()
        )

    timer_status_label = task.status

    if task.status == "Assigned":
        rejected_activity = (
            TaskActivity.query
            .filter_by(
                task_id=task.id,
                action="rejected"
            )
            .order_by(
                TaskActivity.created_at.desc()
            )
            .first()
        )

        if rejected_activity:
            timer_status_label = "Reassigned"

    activities = TaskActivity.query.filter_by(
        task_id=task.id
    ).order_by(
        TaskActivity.created_at.desc()
    ).all()

    comments = (
        TaskComment.query
        .filter_by(
            task_id=task.id,
            parent_id=None
        )
        .order_by(
            TaskComment.created_at.asc()
        )
        .all()
    )

    return render_template(
        "tasks/detail.html",
        # ?panel=1 renders the same page without the app shell so it can
        # be shown inside the task side drawer.
        panel_mode=request.args.get("panel") == "1",
        task=task,
        activities=activities,
        feedbacks=task.feedbacks,
        worked_time=format_seconds(live_seconds),
        live_seconds=live_seconds,
        pending_time=format_seconds(task.pending_seconds),
        in_progress_time=format_seconds(task.in_progress_seconds),
        paused_time=format_seconds(task.paused_seconds),
        on_hold_time=format_seconds(task.on_hold_seconds),
        core_review_time=format_seconds(task.core_review_seconds),
        client_review_time=format_seconds(task.client_review_seconds),
        task_status=task_status,
        can_manage_tasks=has_permission(current_user, "manage_tasks"),
        current_status_seconds=current_status_seconds,
        current_status=task.status,
        timer_status_label=timer_status_label,
        timedelta=timedelta,
        comments=comments,
        reference_files=reference_files,
        working_files=working_files,
        final_files=final_files,
        submission_files=submission_files,
    )

def _can_view_task_file(task_file):
    """Same rule the preview and download routes apply."""

    if has_permission(current_user, "manage_tasks"):
        return True

    task = task_file.task

    return (
        task.assigned_to_id == current_user.id
        or current_user in task.visible_to
    )


@tasks_bp.route("/files/<int:file_id>/thumb")
@login_required
def task_file_thumbnail(file_id):
    """Small derived image for grids and file lists.

    Falls back to the original only when there is genuinely no
    thumbnail to serve (a format Pillow can't read). Generation is
    normally done by the background worker at upload time; doing it
    here as well means files that predate thumbnails, or that the
    worker missed, heal themselves the first time they are shown.
    """

    task_file = TaskFile.query.get_or_404(file_id)

    if not _can_view_task_file(task_file):
        abort(403)

    if (
        task_file.thumbnail_state == thumbnails.STATE_PENDING
        and thumbnails.supports(task_file)
    ):
        thumbnails.generate(task_file.id)
        db.session.refresh(task_file)

    key = task_file.thumbnail_key

    if task_file.thumbnail_state != thumbnails.STATE_READY or not key:
        # Nothing renderable was produced. Sending the original keeps
        # the tile working; it is only reached for formats Pillow
        # could not decode, never for the common ones.
        key = task_file.object_key

    try:
        storage = StorageService()

        url = storage.preview_url(
            object_key=key,
            expires_in=THUMBNAIL_URL_TTL,
        )

    except StorageServiceError:
        current_app.logger.exception(
            "Unable to generate thumbnail URL for task file %s.",
            task_file.id,
        )
        abort(404)

    response = redirect(url)

    # The thumbnail for a given file id never changes content, so let
    # the browser keep it rather than re-walking this route for every
    # tile on every visit. Kept under the signed URL's own lifetime.
    response.headers["Cache-Control"] = (
        f"private, max-age={THUMBNAIL_URL_TTL - 60}"
    )

    return response


@tasks_bp.route("/files/<int:file_id>/preview")
@login_required
def preview_task_file(file_id):

    task_file = TaskFile.query.get_or_404(file_id)
    task = task_file.task

    if not has_permission(current_user, "manage_tasks"):

        can_view = (
            task.assigned_to_id == current_user.id
            or current_user in task.visible_to
        )

        if not can_view:
            flash(
                "You are not allowed to view this file.",
                "error",
            )

            return redirect(
                url_for("tasks.list_tasks")
            )

    try:
        storage = StorageService()

        preview_url = storage.preview_url(
            object_key=task_file.object_key,
            expires_in=600,
        )

    except StorageServiceError:
        current_app.logger.exception(
            "Unable to generate preview URL for task file %s.",
            task_file.id,
        )

        flash(
            "File preview is currently unavailable.",
            "error",
        )

        return redirect(
            url_for(
                "tasks.task_detail",
                task_id=task.id,
            )
        )

    except Exception:
        current_app.logger.exception(
            "Unexpected file preview failure for task file %s.",
            task_file.id,
        )

        flash(
            "File preview is currently unavailable.",
            "error",
        )

        return redirect(
            url_for(
                "tasks.task_detail",
                task_id=task.id,
            )
        )

    return redirect(
        preview_url
    )
@tasks_bp.route("/files/<int:file_id>/download")
@login_required
def download_task_file(file_id):

    task_file = TaskFile.query.get_or_404(file_id)
    task = task_file.task

    if not has_permission(current_user, "manage_tasks"):

        can_view = (
            task.assigned_to_id == current_user.id
            or current_user in task.visible_to
        )

        if not can_view:
            flash(
                "You are not allowed to download this file.",
                "error",
            )

            return redirect(
                url_for("tasks.list_tasks")
            )

    try:
        storage = StorageService()

        download_url = storage.download_url(
            object_key=task_file.object_key,
            download_filename=task_file.original_filename,
            expires_in=600,
        )

    except StorageServiceError:
        current_app.logger.exception(
            "Unable to generate download URL for task file %s.",
            task_file.id,
        )

        flash(
            "File download is currently unavailable.",
            "error",
        )

        return redirect(
            url_for(
                "tasks.task_detail",
                task_id=task.id,
            )
        )

    except Exception:
        current_app.logger.exception(
            "Unexpected file download failure for task file %s.",
            task_file.id,
        )

        flash(
            "File download is currently unavailable.",
            "error",
        )

        return redirect(
            url_for(
                "tasks.task_detail",
                task_id=task.id,
            )
        )

    return redirect(
        download_url
    )


@tasks_bp.route("/files/<int:file_id>/delete", methods=["POST"])
@login_required
def delete_task_file(file_id):

    task_file = TaskFile.query.get_or_404(file_id)
    task = task_file.task

    can_delete = (
        has_permission(current_user, "manage_tasks")
        or task_file.uploaded_by_id == current_user.id
    )

    if not can_delete:
        flash(
            "You are not allowed to delete this file.",
            "error",
        )
        return redirect(
            url_for(
                "tasks.task_detail",
                task_id=task.id,
            )
        )

    filename = task_file.original_filename
    object_key = task_file.object_key

    try:
        db.session.delete(task_file)

        add_activity(
            task,
            action="file_deleted",
            message=f'{current_user.name} deleted file "{filename}".',
        )

        db.session.commit()

    except Exception:
        db.session.rollback()

        current_app.logger.exception(
            "Unable to delete task file %s.",
            file_id,
        )

        flash(
            "Unable to delete the file. Please try again.",
            "error",
        )

        return redirect(
            url_for(
                "tasks.task_detail",
                task_id=task.id,
            )
        )

    try:
        StorageService().delete(object_key=object_key)

    except Exception:
        current_app.logger.exception(
            "Unable to remove storage object for deleted "
            "task file %s: %s",
            file_id,
            object_key,
        )

    flash(
        "File deleted.",
        "success",
    )

    return redirect(
        url_for(
            "tasks.task_detail",
            task_id=task.id,
        )
    )


@tasks_bp.route("/<int:task_id>/upload-submission", methods=["POST"])
@login_required
def upload_submission(task_id):

    task = Task.query.get_or_404(task_id)

    if task.assigned_to_id != current_user.id:
        flash(
            "Only the assigned employee can upload submission files.",
            "error",
        )
        return redirect(
            url_for(
                "tasks.task_detail",
                task_id=task.id,
            )
        )

    submission_files = request.files.getlist(
        "submission_files"
    )

    if not submission_files or not submission_files[0].filename:
        flash(
            "Please select at least one file.",
            "error",
        )
        return redirect(
            url_for(
                "tasks.task_detail",
                task_id=task.id,
            )
        )

    uploaded_count = 0

    storage = StorageService()

    try:

        for submission_file in submission_files:

            if not submission_file.filename:
                continue

            storage.upload_task_file(
                task=task,
                file_storage=submission_file,
                uploaded_by_id=current_user.id,
                folder_type="submission",
                is_final=False,
            )

            uploaded_count += 1

        add_activity(
            task,
            action="submission_uploaded",
            message=(
                f"{current_user.name} uploaded "
                f"{uploaded_count} submission file(s)."
            ),
        )

        if task.created_by_id != current_user.id:

            create_notification(
                user_id=task.created_by_id,
                title="Task submission uploaded",
                message=(
                    f"{current_user.name} uploaded files for "
                    f"'{task.title}'."
                ),
                link=url_for(
                    "tasks.task_detail",
                    task_id=task.id,
                ),
                actor_id=current_user.id,
                task_id=task.id,
            )

        db.session.commit()

    except StorageServiceError as error:

        db.session.rollback()

        current_app.logger.exception(
            "Submission upload failed."
        )

        flash(
            f"Submission upload failed: {error}",
            "error",
        )

        return redirect(
            url_for(
                "tasks.task_detail",
                task_id=task.id,
            )
        )

    except Exception as error:

        db.session.rollback()

        current_app.logger.exception(
            "Submission upload failed for task %s.",
            task.id,
        )

        flash(
            str(error),
            "error",
        )

        return redirect(
            url_for(
                "tasks.task_detail",
                task_id=task.id,
            )
        )

    flash(
        f"{uploaded_count} submission file(s) uploaded successfully.",
        "success",
    )

    return redirect(
        url_for(
            "tasks.task_detail",
            task_id=task.id,
        )
    )


def _require_submission_uploader(task):

    if task.assigned_to_id != current_user.id:
        return jsonify(
            success=False,
            message="Only the assigned employee can upload submission files.",
        ), 403

    return None


@tasks_bp.route(
    "/<int:task_id>/multipart/initiate",
    methods=["POST"],
)
@login_required
def initiate_submission_multipart_upload(task_id):

    task = Task.query.get_or_404(task_id)

    permission_error = _require_submission_uploader(task)

    if permission_error:
        return permission_error

    data = request.get_json(silent=True) or {}

    filename = data.get("filename")
    content_type = data.get("content_type")

    storage = StorageService()

    try:
        upload_session = storage.initiate_task_file_multipart_upload(
            task=task,
            filename=filename,
            folder_type="submission",
            uploaded_by_id=current_user.id,
            content_type=content_type,
        )

    except StorageServiceError as error:
        return jsonify(
            success=False,
            message=str(error),
        ), 400

    return jsonify(
        success=True,
        upload_id=upload_session["upload_id"],
        object_key=upload_session["object_key"],
        stored_filename=upload_session["stored_filename"],
        original_filename=upload_session["original_filename"],
    )


@tasks_bp.route(
    "/<int:task_id>/multipart/part-url",
    methods=["POST"],
)
@login_required
def get_submission_multipart_part_url(task_id):

    task = Task.query.get_or_404(task_id)

    permission_error = _require_submission_uploader(task)

    if permission_error:
        return permission_error

    data = request.get_json(silent=True) or {}

    object_key = data.get("object_key")
    upload_id = data.get("upload_id")
    part_number = data.get("part_number")

    storage = StorageService()

    try:
        part_url = storage.get_multipart_part_url(
            object_key=object_key,
            upload_id=upload_id,
            part_number=part_number,
        )

    except StorageServiceError as error:
        return jsonify(
            success=False,
            message=str(error),
        ), 400

    return jsonify(
        success=True,
        url=part_url,
    )


@tasks_bp.route(
    "/<int:task_id>/multipart/complete",
    methods=["POST"],
)
@login_required
def complete_submission_multipart_upload(task_id):

    task = Task.query.get_or_404(task_id)

    permission_error = _require_submission_uploader(task)

    if permission_error:
        return permission_error

    data = request.get_json(silent=True) or {}

    object_key = data.get("object_key")
    upload_id = data.get("upload_id")
    parts = data.get("parts")
    original_filename = data.get("original_filename")
    stored_filename = data.get("stored_filename")

    storage = StorageService()

    try:
        complete_result = storage.complete_task_file_multipart_upload(
            object_key=object_key,
            upload_id=upload_id,
            parts=parts,
            task=task,
            uploaded_by_id=current_user.id,
            folder_type="submission",
            original_filename=original_filename,
            stored_filename=stored_filename,
            is_final=False,
        )

        task_file = complete_result["task_file"]

        add_activity(
            task,
            action="submission_uploaded",
            message=(
                f"{current_user.name} uploaded "
                f"submission file: {task_file.original_filename}"
            ),
        )

        if task.created_by_id != current_user.id:

            create_notification(
                user_id=task.created_by_id,
                title="Task submission uploaded",
                message=(
                    f"{current_user.name} uploaded files for "
                    f"'{task.title}'."
                ),
                link=url_for(
                    "tasks.task_detail",
                    task_id=task.id,
                ),
                actor_id=current_user.id,
                task_id=task.id,
            )

        db.session.commit()

    except StorageServiceError as error:

        db.session.rollback()

        current_app.logger.exception(
            "Submission multipart upload completion failed."
        )

        return jsonify(
            success=False,
            message=str(error),
        ), 400

    return jsonify(
        success=True,
        file={
            "id": task_file.id,
            "filename": task_file.original_filename,
            "preview_url": url_for(
                "tasks.preview_task_file",
                file_id=task_file.id,
            ),
            "download_url": url_for(
                "tasks.download_task_file",
                file_id=task_file.id,
            ),
        },
    )


@tasks_bp.route(
    "/<int:task_id>/multipart/abort",
    methods=["POST"],
)
@login_required
def abort_submission_multipart_upload(task_id):

    task = Task.query.get_or_404(task_id)

    permission_error = _require_submission_uploader(task)

    if permission_error:
        return permission_error

    data = request.get_json(silent=True) or {}

    object_key = data.get("object_key")
    upload_id = data.get("upload_id")

    storage = StorageService()

    try:
        storage.abort_task_file_multipart_upload(
            object_key=object_key,
            upload_id=upload_id,
        )

    except StorageServiceError as error:
        return jsonify(
            success=False,
            message=str(error),
        ), 400

    return jsonify(success=True)


@tasks_bp.route(
    "/<int:task_id>/comment",
    methods=["POST"]
)
@login_required
def add_comment(task_id):

    task = Task.query.get_or_404(task_id)

    message = request.form.get(
        "message",
        ""
    ).strip()

    if not message:

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "success": False,
                "message": "Comment cannot be empty."
            }), 400

        flash("Comment cannot be empty.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    comment = TaskComment(
        task_id=task.id,
        user_id=current_user.id,
        message=message,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )

    db.session.add(comment)
    db.session.flush()

    add_activity(
        task,
        action="comment",
        message=f"{current_user.name} added a comment."
    )

    if task.assigned_to_id != current_user.id:
        create_notification(
            user_id=task.assigned_to_id,
            title="New Comment",
            message=f"{current_user.name} commented on '{task.title}'",
            link=url_for("tasks.task_detail", task_id=task.id),
            actor_id=current_user.id,
            task_id=task.id
        )

    db.session.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "success": True,
            "comment": {
                "id": comment.id,
                "user_id": comment.user_id,
                "user_name": comment.user.name,
                "avatar": comment.user.name[:1].upper(),
                "message": comment.message,
                "time": (comment.created_at + timedelta(hours=5, minutes=30)).strftime("%d %b %Y â€¢ %I:%M %p"),
                "can_edit": comment.user_id == current_user.id
            }
        })

    flash("Comment added.", "success")
    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route(
    "/comments/<int:comment_id>/reply",
    methods=["POST"]
)
@login_required
def reply_comment(comment_id):

    comment = TaskComment.query.get_or_404(comment_id)
    task = comment.task

    message = request.form.get("message", "").strip()

    if not message:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "success": False,
                "message": "Reply cannot be empty."
            }), 400

        flash("Reply cannot be empty.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    reply = TaskComment(
        task_id=task.id,
        user_id=current_user.id,
        parent_id=comment.id,
        message=message,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )

    db.session.add(reply)
    db.session.flush()

    add_activity(
        task,
        action="comment",
        message=f"{current_user.name} replied to a comment."
    )

    if comment.user_id != current_user.id:
        create_notification(
            user_id=comment.user_id,
            title="New Reply",
            message=f"{current_user.name} replied to your comment.",
            link=url_for("tasks.task_detail", task_id=task.id),
            actor_id=current_user.id,
            task_id=task.id
        )

    db.session.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "success": True,
            "reply": {
                "id": reply.id,
                "parent_id": comment.id,
                "user_id": reply.user_id,
                "user_name": reply.user.name,
                "avatar": reply.user.name[:1].upper(),
                "message": reply.message,
                "time": (reply.created_at + timedelta(hours=5, minutes=30)).strftime("%d %b %Y â€¢ %I:%M %p"),
                "can_edit": reply.user_id == current_user.id
            }
        })

    flash("Reply added.", "success")
    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route("/comments/<int:comment_id>/edit", methods=["POST"])
@login_required
def edit_comment(comment_id):

    comment = TaskComment.query.get_or_404(comment_id)
    task = comment.task

    if comment.user_id != current_user.id:
        flash("You can edit only your own comment.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    message = request.form.get("message", "").strip()

    if not message:
        flash("Comment cannot be empty.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    comment.message = message
    comment.is_edited = True
    comment.updated_at = datetime.utcnow()

    add_activity(
        task,
        action="comment",
        message=f"{current_user.name} edited a comment."
    )

    db.session.commit()

    flash("Comment updated.", "success")

    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route("/comments/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_comment(comment_id):

    comment = TaskComment.query.get_or_404(comment_id)
    task = comment.task

    if comment.user_id != current_user.id:
        flash("You can delete only your own comment.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    db.session.delete(comment)

    add_activity(
        task,
        action="comment",
        message=f"{current_user.name} deleted a comment."
    )

    db.session.commit()

    flash("Comment deleted.", "success")

    return redirect(url_for("tasks.task_detail", task_id=task.id))
