import os
from werkzeug.utils import secure_filename
from app.utils.timezone import ist_now
from datetime import datetime, timedelta

from flask import (
    Blueprint,
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


tasks_bp = Blueprint("tasks", __name__, url_prefix="/tasks")

from app.storage.storage_service import (
    StorageService,
    StorageServiceError,
)

from app.models import TaskFile


def generate_task_code():

    sequence = TaskSequence.query.get(1)

    if not sequence:

        sequence = TaskSequence(
            id=1,
            last_code=1000
        )

        db.session.add(sequence)
        db.session.flush()

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

    if task.status == "Assigned":
        task.pending_seconds = (
            task.pending_seconds or 0
        ) + elapsed

    elif task.status == "In Progress":
        task.in_progress_seconds = (
            task.in_progress_seconds or 0
        ) + elapsed

    elif task.status == "Paused":
        task.hold_seconds = (
            task.hold_seconds or 0
        ) + elapsed

    elif task.status == "Core Review":
        task.core_review_seconds = (
            task.core_review_seconds or 0
        ) + elapsed

    elif task.status == "Client Review":
        task.client_review_seconds = (
            task.client_review_seconds or 0
        ) + elapsed

    elif task.status == "Published":
        task.published_seconds = (
            task.published_seconds or 0
        ) + elapsed

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

    query = get_task_base_query()

    if selected_status:
        query = query.filter(Task.status == selected_status)

    if selected_priority:
        query = query.filter(Task.priority == selected_priority)

    query = apply_task_search(query, search)

        # =====================================
    # FILTER BY
    # =====================================

    today = ist_now()

    if filter_by == "today":

        query = query.filter(
            db.func.date(Task.created_at) == today.date()
        )

    elif filter_by == "yesterday":

        yesterday = today.date() - timedelta(days=1)

        query = query.filter(
            db.func.date(Task.created_at) == yesterday
        )

    elif filter_by == "last_7_days":

        query = query.filter(
            Task.created_at >= today - timedelta(days=7)
        )

    elif filter_by == "last_30_days":

        query = query.filter(
            Task.created_at >= today - timedelta(days=30)
        )

    elif filter_by == "this_month":

        query = query.filter(
            db.extract("month", Task.created_at) == today.month,
            db.extract("year", Task.created_at) == today.year
        )

    elif filter_by == "last_90_days":

        query = query.filter(
            Task.created_at >= today - timedelta(days=90)
        )

    if assigned_to:

        query = query.filter(
            Task.assigned_to_id == int(assigned_to)
        )

    if assigned_by:

        query = query.filter(
            Task.created_by_id == int(assigned_by)
        )

    if client_id:

        query = query.filter(
            Task.client_id == int(client_id)
        )

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

    else:

        query = query.order_by(Task.id.desc())

    tasks = query.all()

    statuses = [
        "Assigned",
        "In Progress",
        "Paused",
        "Core Review",
        "Client Review",
        "Published"
    ]

    priorities = [
        "Low",
        "Medium",
        "High",
        "Urgent"
    ]

    board_columns = {
        status: []
        for status in statuses
    }

    for task in tasks:
        board_columns.setdefault(task.status, []).append(task)

    total_tasks = len(tasks)

    completed_tasks = len([
        task for task in tasks
        if task.employee_completed
    ])

    review_tasks = len([
        task for task in tasks
        if task.status in ["Core Review", "Client Review"]
    ])

    overdue_tasks = len([
        task for task in tasks
        if task.deadline
        and task.deadline < ist_now()
            and task.status in ["Assigned", "In Progress", "Paused"]
    ])

    return render_template(
        "tasks/list.html",
        tasks=tasks,
        board_columns=board_columns,
        statuses=statuses,
        priorities=priorities,
        selected_status=selected_status,
        selected_priority=selected_priority,
        search=search,
        total_tasks=total_tasks,
        completed_tasks=completed_tasks,
        review_tasks=review_tasks,
        overdue_tasks=overdue_tasks
    )

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


@tasks_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_task():

    if not has_permission(current_user, "manage_tasks"):
        flash(
            (
                "You don't have permission to assign tasks. "
                "You can self assign your own task."
            ),
            "error",
        )

        return redirect(
            url_for("tasks.self_assign_task")
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
                    url_for("tasks.add_task")
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
                url_for("tasks.add_task")
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
                url_for("tasks.add_task")
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
                url_for("tasks.add_task")
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
                url_for("tasks.add_task")
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
                url_for("tasks.add_task")
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
                url_for("tasks.add_task")
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
                url_for("tasks.add_task")
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
                url_for("tasks.add_task")
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
                url_for("tasks.add_task")
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
                url_for("tasks.add_task")
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
        clients=clients,
        deliverables=deliverables,
        employees=employees,
        deadline_default=deadline_default,
    )

@tasks_bp.route("/self-assign", methods=["GET", "POST"])
@login_required
def self_assign_task():

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
            return redirect(url_for("tasks.self_assign_task"))

        try:
            quantity = float(request.form.get("quantity") or 1)
            estimated_time = float(request.form.get("estimated_time") or 1)

        except (TypeError, ValueError):
            flash("Quantity and estimated time must be valid.", "error")
            return redirect(url_for("tasks.self_assign_task"))

        if quantity <= 0 or estimated_time <= 0:
            flash("Quantity and estimated time must be greater than zero.", "error")
            return redirect(url_for("tasks.self_assign_task"))

        deliverable = ClientDeliverable.query.get(deliverable_id)

        if not deliverable or not deliverable.monthly_target:
            flash("Invalid deliverable selected.", "error")
            return redirect(url_for("tasks.self_assign_task"))

        if deliverable.monthly_target.client_id != client_id:
            flash("Selected deliverable does not belong to selected client.", "error")
            return redirect(url_for("tasks.self_assign_task"))

        title = request.form.get("title", "").strip()

        if not title:
            flash("Task title is required.", "error")
            return redirect(url_for("tasks.self_assign_task"))

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

            return redirect(url_for("tasks.self_assign_task"))

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

            return redirect(url_for("tasks.self_assign_task"))

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

        allowed_statuses = [
            "Assigned",
            "In Progress",
            "Paused",
            "Core Review",
            "Client Review",
            "Published"
        ]

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
        employees=employees
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
        task.assigned_to_id == current_user.id
        and task.status == "Core Review"
    ):
        employee_allowed_statuses = [
            "Assigned",
            "In Progress",
            "Hold",
        ]

        if new_status in employee_allowed_statuses:
            pass

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

    allowed_status = [
        "Assigned",
        "In Progress",
        "Paused",
        "Core Review",
        "Client Review",
        "Published",
    ]

    # ---------------------------------------
    # Employee Drag Rules
    # ---------------------------------------

    if not has_permission(current_user, "manage_tasks"):

        allowed_moves = {

            "Assigned": [
                "In Progress"
            ],

            "In Progress": [
                "Paused",
                "Core Review"
            ],

            "Paused": [
                "In Progress"
            ],

            # Employee can pull back a mistaken submission
            "Core Review": [
                "Assigned",
                "In Progress",
                "Paused",
            ],

            "Client Review": [],

            "Published": []

        }

        if new_status not in allowed_moves.get(task.status, []):

            return jsonify({

                "success": False,

                "message": (
                    "You cannot move this task to that status."
                )

            }), 403

    if new_status not in allowed_status:

        return jsonify(
            {
                "success": False,
                "message": "Invalid status."
            }
        ), 400

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
        task=task,
        activities=activities,
        feedbacks=task.feedbacks,
        worked_time=format_seconds(live_seconds),
        live_seconds=live_seconds,
        pending_time=format_seconds(task.pending_seconds),
        in_progress_time=format_seconds(task.in_progress_seconds),
        hold_time=format_seconds(task.hold_seconds),
        core_review_time=format_seconds(task.core_review_seconds),
        client_review_time=format_seconds(task.client_review_seconds),
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
