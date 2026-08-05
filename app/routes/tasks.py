import os
import csv
import io
import math
from uuid import uuid4
from werkzeug.utils import secure_filename
from app.utils.timezone import ist_now, ist_date
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
    Response,
)

from flask_login import login_required, current_user

from sqlalchemy import or_, cast, String
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from app.extensions import db, limiter
from app.models import (
    Task,
    Client,
    ClientDeliverable,
    User,
    TaskFeedback,
    TaskActivity,
    TaskSequence,
    TaskComment,
    TaskFile,
    TaskTransferRequest
)
from app.utils.permissions import (
    can_assign_tasks, can_publish, can_review, can_use_social,
    can_view_all_tasks, has_permission,
)
from app.utils import roles
from app.utils.notifications import create_notification
from app.utils.mentions import notify_mentioned_users, find_mentioned_users
from app.utils import task_status
from app.utils import social_platforms as social


tasks_bp = Blueprint("tasks", __name__, url_prefix="/tasks")

from app.storage.storage_service import (
    StorageService,
    StorageServiceError,
)

from app.services import thumbnails
from app.services import deliverables

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

    # Only start the clock if it isn't already running. Overwriting a live
    # timer_started_at (e.g. a double-submit of Start on an In Progress task)
    # would silently discard every second worked since the last start.
    if task.timer_started_at is None:
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
    elif elapsed > 0:
        # The table-driven lookup does not by itself prevent the failure the
        # comment above describes: duration_field() returns None quietly and
        # the branch above just skips, so a status with no bucket loses its
        # time exactly the way "Hold" did. A status in ALL_STATUSES missing
        # from DURATION_FIELD is caught by tests before it ships; this covers
        # the other route in - a literal typed straight into the database, or
        # written by a path that never consulted the catalog - where the only
        # symptom is time quietly going missing from someone's timesheet.
        current_app.logger.warning(
            "task %s left status %r with no duration bucket - %ds of tracked "
            "time discarded", task.id, task.status, elapsed,
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

    # Leaving Published (a manager reworking a delivered task) reverses the
    # completion side-effects, so throughput metrics and the deliverable tally
    # stop counting a task that is being redone. Guarded on completed_at so we
    # only ever undo a completion that was actually recorded.
    if task.status == task_status.PUBLISHED \
            and new_status != task_status.PUBLISHED and task.completed_at:
        task.completed_at = None
        # Unconditional, unlike the `if deliverable.completed_count:` this
        # replaces. That guard skipped the subtraction whenever the stored
        # count was already 0 - but still cleared completed_at, so the next
        # approval added 1 back and a rework cycle netted +1 with no delivery.
        # deliverables.adjust_count clamps at 0 inside the row lock, which is
        # what the guard was reaching for without the race.
        deliverables.adjust_count(task.deliverable_id, -1)

    task.status = new_status
    task.status_started_at = now

    return old_status


def _social_needs_handoff(task):
    """A social task, with the engine on, must reach Published through the
    Social Studio handoff (Approve & Send / Mark manually published) - never a
    bare board drag or edit-dropdown pick, which would skip the publish gate."""
    return bool(
        task.is_social_media
        and current_app.config.get("SOCIAL_ENGINE_ENABLED")
    )


def apply_completion_effects(task, new_status):
    """The completion side-effects a status change into a review/done state
    must carry, applied identically no matter which path triggered it (approve
    button, edit dropdown, or kanban drag). Idempotent: completed_at gates the
    one-time deliverable count so a status re-entry never double-counts.

    Assumes record_status_time has ALREADY moved task.status to new_status.
    """
    if new_status in ("Core Review", "Client Review", "Published"):
        task.employee_completed = True
        if not task.employee_completed_at:
            task.employee_completed_at = ist_now()
    elif task.employee_completed:
        # Moved back into an active/working state (rework after a review, an
        # undo, or a void): it is no longer "completed by the assignee", so
        # clear the flag - otherwise a reworked task keeps counting as
        # completed in the KPIs and the "Completed" filter. reject_task already
        # does this; centralising it here covers the edit-dropdown and board-
        # drag paths too, which previously left the phantom flag set.
        task.employee_completed = False
        task.employee_completed_at = None

    if new_status == "Published" and not task.completed_at:
        task.completed_at = ist_now()
        deliverables.adjust_count(task.deliverable_id, +1)
        if task.is_social_media and task.social_platforms \
                and not task.social_platforms_published:
            task.social_platforms_published = task.social_platforms


def _notify_reviewers(task):
    """Tell everyone who can approve that a task is waiting in review. Shared by
    submit_review and the board-drag path so both notify identically."""
    reviewers = [
        u for u in User.query.filter_by(status="active").all()
        if (has_permission(u, "approve_tasks")
            or has_permission(u, "publish_tasks")) and u.id != current_user.id
    ]
    for reviewer in reviewers:
        create_notification(
            user_id=reviewer.id, title="Review requested",
            message=f"{current_user.name} submitted: {task.title}",
            link=url_for("tasks.task_detail", task_id=task.id),
            actor_id=current_user.id, task_id=task.id)


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


def parse_fallback_fields(assigned_to_id):
    """Read backup_assignee_id / fallback_hours from the task form.

    Both are optional, but half-set (one without the other) is
    rejected rather than silently ignored - a manager who fills in
    just the hours would otherwise get no fallback at all with no
    indication why.

    Returns (backup_assignee_id, fallback_hours, error_message).
    On success error_message is None. On failure both values are
    None and error_message explains what to fix.
    """

    backup_raw = (request.form.get("backup_assignee_id") or "").strip()
    hours_raw = (request.form.get("fallback_hours") or "").strip()

    if not backup_raw and not hours_raw:
        return None, None, None

    if backup_raw and not hours_raw:
        return None, None, (
            "Set a fallback window (hours) to use a backup assignee."
        )

    if hours_raw and not backup_raw:
        return None, None, (
            "Select a backup assignee to use a fallback window."
        )

    try:
        backup_assignee_id = int(backup_raw)
        fallback_hours = int(hours_raw)
    except (TypeError, ValueError):
        return None, None, "Fallback window must be a whole number of hours."

    if fallback_hours <= 0:
        return None, None, "Fallback window must be greater than zero."

    if backup_assignee_id == assigned_to_id:
        return None, None, (
            "Backup assignee must be different from the assignee."
        )

    backup_user = User.query.filter(
        User.id == backup_assignee_id, User.status == "active",
        User.role.in_(roles.ALL_ROLE_VALUES)
    ).first()

    if not backup_user:
        return None, None, "Selected backup assignee is invalid."

    return backup_assignee_id, fallback_hours, None


def active_user_names():
    """Names offered to the @-mention picker (window.MENTION_USERS) -
    every active user regardless of role, matching who find_mentioned_users
    actually matches against."""
    return [
        u.name for u in User.query
        .filter_by(status="active")
        .order_by(User.name.asc())
        .all()
    ]


def parse_social_media_fields():
    """Read is_social_media / social_platforms from the task form.

    Returns (is_social_media, platforms_csv, error_message). platforms_csv
    is the comma-joined, catalog-ordered string to store on the task -
    empty when the task isn't social media. On failure error_message
    explains what to fix and the other two values are None.
    """

    # The VALUE decides, not merely whether the field was sent. The form
    # asks "Is this a social media post?" as Yes/No, and No posts "0" -
    # which a plain bool() would read as true, quietly marking every task
    # social. Kept tolerant of the checkbox spelling ("1"/absent) so any
    # older form or no-JS path still behaves.
    raw = (request.form.get("is_social_media") or "").strip().lower()
    is_social_media = raw in {"1", "true", "yes", "on"}

    if not is_social_media:
        return False, "", None

    selected = request.form.getlist("social_platforms")
    platforms = social.parse_platforms(",".join(selected))

    if not platforms:
        return None, None, (
            "Select at least one platform, or answer No to "
            '"Is this a social media post?"'
        )

    return True, social.format_platforms(platforms), None


def _like_needle(term):
    """A LIKE pattern for one word, with the wildcards escaped.

    Unescaped, a search for "%" matched every task and "_" matched any
    single character - the two characters a user is most likely to paste in
    from a real title without meaning anything by them.
    """
    escaped = (
        term.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
    )
    return f"%{escaped}%"


def apply_task_search(query, search):
    """Narrow `query` to the tasks matching a free-text search.

    The search is split into words and *every* word must match - but each
    one is free to match a different column. A single LIKE over the whole
    string can only ever find text that sits in one column, so "rishu 12"
    (name on User, number on Task) and "hope regression" (client vs
    deliverable) both came back empty even though they name one task
    exactly. That reads as the search being fussy about how you type it.
    Word order does not matter, and matching is case-insensitive
    throughout.
    """
    terms = search.split() if search else []

    if not terms:
        return query

    # outerjoin, not join: an inner join here silently dropped any task with
    # no client or deliverable from every search - even when its title or
    # code matched - because the row had nothing to join to. With an outer
    # join those columns are simply NULL (so their ilike clauses don't
    # match), and the task still surfaces on a title/code/status hit. This
    # matches the outer-join behaviour the global search already uses.
    query = query.outerjoin(
        Client,
        Task.client_id == Client.id
    ).outerjoin(
        ClientDeliverable,
        Task.deliverable_id == ClientDeliverable.id
    ).outerjoin(
        User,
        Task.assigned_to_id == User.id
    )

    for term in terms:
        needle = _like_needle(term)

        clauses = [
            Task.title.ilike(needle, escape="\\"),
            Task.description.ilike(needle, escape="\\"),
            Task.status.ilike(needle, escape="\\"),
            Task.priority.ilike(needle, escape="\\"),

            Client.client_name.ilike(needle, escape="\\"),

            ClientDeliverable.service_name.ilike(needle, escape="\\"),
            ClientDeliverable.deliverable_name.ilike(needle, escape="\\"),

            User.name.ilike(needle, escape="\\"),
            User.email.ilike(needle, escape="\\"),
        ]

        # "#1012" and "1012" are the same task to everyone but the database.
        # Guarded: a bare "#" would otherwise leave an empty needle that
        # matches every code, quietly turning that word into a wildcard.
        code = term.replace("#", "")
        if code:
            clauses.append(
                cast(Task.task_code, String).ilike(
                    _like_needle(code), escape="\\"
                )
            )

        query = query.filter(or_(*clauses))

    return query


def can_view_task(task):
    """May the signed-in user see this task at all?

    The same rule task_detail applies, lifted out so the write paths can
    ask it too. add_comment and reply_comment never did: the page was
    scoped but the POST behind it was not, so any signed-in user could
    comment on any task id - and @-mention people from a task they could
    not open.

    The last clause is the transfer one. Being asked to take a task over
    has to come with sight of it: the Accept and Decline buttons live on
    the task page, and the person being asked is - by definition - not
    the assignee yet. The query runs only when the cheap in-memory checks
    have already failed, so the common path never touches the database
    for it.
    """
    if task is None:
        return False

    if can_view_all_tasks(current_user):
        return True

    if (task.assigned_to_id == current_user.id
            or current_user in task.visible_to):
        return True

    return TaskTransferRequest.is_pending_party(task.id, current_user.id)


def _task_access_denied(task_id):
    """The answer to "you may not open this task".

    Two shapes, because there are two ways in. Inside the task drawer the
    reply must stay on the SAME url: task-panel.js watches the iframe's
    path and treats a change as "the panel left the task", so a redirect
    made the drawer close itself and hard-reload the page behind it - the
    user saw a panel flash open and vanish, with nothing said. Rendering a
    403 at the same address keeps the drawer open and lets it explain.

    Everywhere else it is a flash and a redirect. The redirect was already
    there; the flash was not, so a plain navigation dumped people on the
    task list with no idea why.
    """
    message = (
        "You do not have access to this task. If someone has asked you to "
        "take it over, open the request from your notifications."
    )

    if request.args.get("panel") == "1":
        return render_template(
            "tasks/_no_access_panel.html",
            message=message,
            task_id=task_id,
        ), 403

    flash(message, "error")
    return redirect(url_for("tasks.list_tasks"))


def get_task_base_query():
    """Tasks this user may see, as a query.

    Mirrors can_view_task, including the pending-transfer clause - a task
    someone has been asked to take belongs on their board while they
    decide, not only behind a notification link.
    """

    if can_view_all_tasks(current_user):
        return Task.query

    return Task.query.filter(
        db.or_(
            Task.assigned_to_id == current_user.id,
            Task.visible_to.any(User.id == current_user.id),
            Task.transfer_requests.any(
                db.and_(
                    TaskTransferRequest.status == TaskTransferRequest.PENDING,
                    db.or_(
                        TaskTransferRequest.to_user_id == current_user.id,
                        TaskTransferRequest.from_user_id == current_user.id,
                    ),
                )
            ),
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
        # ?status= accepts either one exact status or the name of a
        # group (see task_status.STATUS_GROUPS), so a dashboard card
        # that counts several statuses can link to a filter covering
        # all of them instead of just the first.
        group = task_status.group_members(selected_status)

        if group:
            query = query.filter(Task.status.in_(group))
        else:
            query = query.filter(Task.status == selected_status)

    if selected_priority:
        query = query.filter(Task.priority == selected_priority)

    query = apply_task_search(query, search)

    today = ist_now()

    if filter_by == "today":
        query = query.filter(ist_date(Task.created_at) == today.date())

    elif filter_by == "yesterday":
        query = query.filter(
            ist_date(Task.created_at) == today.date() - timedelta(days=1)
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


def apply_task_scope(query, args):
    """Structural scope ONLY - which people's / which client's tasks are in
    view (My Tasks, a client, an assigner). Deliberately excludes the
    transient list-slice filters (status / priority / search / date range),
    unlike apply_task_filters().

    The headline KPI cards use this so they stay a stable overview of the
    scope: filtering the board to "In Progress" should not zero out the
    "In Review", "Completed" and "Overdue" cards and make them contradict
    their own labels. The board/list below still uses apply_task_filters()."""

    assigned_to = args.get("assigned_to", "").strip()
    assigned_by = args.get("assigned_by", "").strip()
    client_id = args.get("client", "").strip()

    if assigned_to and assigned_to.isdigit():
        query = query.filter(Task.assigned_to_id == int(assigned_to))

    if assigned_by and assigned_by.isdigit():
        query = query.filter(Task.created_by_id == int(assigned_by))

    if client_id and client_id.isdigit():
        query = query.filter(Task.client_id == int(client_id))

    return query


def compute_task_kpis(scope_query):
    """Headline KPI counts (total / completed / review / overdue) for a
    scope query. Columns-only aggregate - no ORM hydration, no joins - so
    it stays cheap enough to run on every list render and every live poll.

    Void tasks are excluded from every figure (cancelled work is neither
    delivered nor outstanding), and on-hold tasks never count as overdue
    (they are blocked by someone outside the team) - matching the board and
    the previous inline logic exactly."""

    now = ist_now()

    rows = scope_query.with_entities(
        Task.status,
        Task.employee_completed,
        Task.deadline,
    ).all()

    total = completed = review = overdue = 0

    for status, employee_completed, deadline in rows:

        if status in task_status.EXCLUDED_FROM_METRICS:
            continue

        total += 1

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

    return {
        "total": total,
        "completed": completed,
        "review": review,
        "overdue": overdue,
    }


@tasks_bp.route("/export.csv")
@login_required
def export_tasks_csv():
    """Download the current task list as CSV. Reuses the same permission
    scope and filters as the list, so the file matches exactly what the
    page is showing (pass the page's query string straight through)."""

    tasks = (
        apply_task_filters(get_task_base_query(), request.args)
        .order_by(Task.created_at.desc())
        .all()
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "Task Code", "Title", "Client", "Service", "Deliverable",
        "Assigned To", "Assigned By", "Priority", "Status",
        "Deadline", "Created", "Employee Completed", "Worked Hours",
    ])

    for t in tasks:
        writer.writerow([
            t.task_code or "",
            t.title or "",
            t.client.client_name if t.client else "",
            t.deliverable.service_name if t.deliverable else "",
            t.deliverable.deliverable_name if t.deliverable else "",
            t.assigned_to.name if t.assigned_to else "",
            t.created_by.name if t.created_by else "",
            t.priority or "",
            t.status or "",
            t.deadline.strftime("%Y-%m-%d %H:%M") if t.deadline else "",
            t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
            "Yes" if t.employee_completed else "No",
            round((t.worked_seconds or 0) / 3600, 2),
        ])

    filename = f"tasks-{ist_now().strftime('%Y%m%d')}.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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

    # The board / list reconciliation needs exactly the visible (filtered)
    # set, so `tasks` comes from the fully-filtered query.
    query = apply_task_filters(get_task_base_query(), request.args)

    rows = query.with_entities(
        Task.id,
        Task.status,
        Task.priority,
    ).all()

    tasks = {}

    for task_id, status, priority in rows:
        tasks[str(task_id)] = {
            "status": status,
            "priority": priority,
            # Void tasks aren't shown on the board - matching list_tasks().
            "void": status in task_status.EXCLUDED_FROM_METRICS,
        }

    # The KPI cards are a stable scope overview (see compute_task_kpis /
    # list_tasks), so the poll must NOT recompute them from the filtered
    # slice - otherwise a refresh would collapse them all over again. They
    # come from the same scope query the initial render uses.
    counts = compute_task_kpis(
        apply_task_scope(get_task_base_query(), request.args)
    )

    return jsonify(tasks=tasks, counts=counts)


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

    # Eager-load the four relationships every card and row dereferences
    # (client, deliverable, assignee, creator). Without this the board and
    # the table each triggered ~4 extra queries per task - a page of M
    # tasks fired 4xM SELECTs. selectinload issues one extra query per
    # relationship instead (a handful total), and unlike joinedload it adds
    # no JOINs, so it stays compatible with the group_by file-size sorts
    # above and never multiplies rows.
    query = query.options(
        selectinload(Task.client),
        selectinload(Task.deliverable),
        selectinload(Task.assigned_to),
        selectinload(Task.created_by),
    )

    tasks = query.all()

    # Load every Studio post for this page in one query. list.html renders
    # publish_badge twice per task (the table row and the board card), and
    # each call used to issue its own SocialPost query plus a lazy load of
    # that post's targets. Priming here collapses the whole page into two.
    from app.social.services import task_link
    task_link.prime_badges(tasks)

    statuses = task_status.ALL_STATUSES

    # Offered above the individual statuses so arriving from a
    # dashboard card that counts a group ("Needs Review") lands on a
    # filter the dropdown can actually show as selected - and clear.
    status_groups = list(task_status.STATUS_GROUPS.keys())

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

    # Headline KPI cards are a stable overview of the current SCOPE (My
    # Tasks / a client), NOT of the status/priority/search slice the board
    # below is showing. Computing them from the filtered `tasks` made every
    # card collapse the moment a filter was applied - e.g. filtering to
    # "In Progress" zeroed the In Review / Completed / Overdue cards so they
    # contradicted their own labels. They now come from a separate, cheap
    # scope query (the same one the live poll uses).
    kpis = compute_task_kpis(
        apply_task_scope(get_task_base_query(), request.args)
    )

    total_tasks = kpis["total"]
    completed_tasks = kpis["completed"]
    review_tasks = kpis["review"]
    overdue_tasks = kpis["overdue"]

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
        status_groups=status_groups,
        priorities=priorities,
        selected_status=selected_status,
        selected_priority=selected_priority,
        selected_assigned_to=assigned_to,
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

    if not can_view_all_tasks(current_user):

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

    elif filter_type == "due_today":

        page_title = "Due Today"
        page_subtitle = "Active tasks whose deadline is today"

        query = query.filter(
            Task.deadline.isnot(None),
            db.func.date(Task.deadline) == ist_now().date(),
            Task.status.in_([
                "Assigned",
                "In Progress",
                "Paused"
            ])
        )

    elif filter_type == "due_week":

        page_title = "Due This Week"
        page_subtitle = "Active tasks due within the next 7 days"

        now = ist_now()

        query = query.filter(
            Task.deadline.isnot(None),
            Task.deadline >= now,
            Task.deadline <= now + timedelta(days=7),
            Task.status.in_([
                "Assigned",
                "In Progress",
                "Paused"
            ])
        )

    elif filter_type == "unassigned":

        page_title = "Unassigned Tasks"
        page_subtitle = "Tasks with no one assigned yet"

        query = query.filter(Task.assigned_to_id.is_(None))

    elif filter_type == "on_hold":

        page_title = "On Hold"
        page_subtitle = "Tasks currently blocked or parked on hold"

        query = query.filter(Task.status == "On Hold")

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

    tasks = query.options(
        selectinload(Task.client),
        selectinload(Task.deliverable),
        selectinload(Task.assigned_to),
        selectinload(Task.created_by),
    ).order_by(
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

    if not can_assign_tasks(current_user):
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

    clients = Client.ordered_with_sub_clients()

    deliverables = (
        ClientDeliverable.query
        .order_by(ClientDeliverable.id.desc())
        .all()
    )

    employees = (
        User.query
        .filter(
            User.status == "active",
            User.role.in_(roles.ALL_ROLE_VALUES),
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

        # The dropdown restricts assignees to assignable roles, but a tampered
        # POST could target any active user - validate the role server-side too.
        if not User.query.filter(
                User.id == assigned_to_id, User.status == "active",
                User.role.in_(roles.ALL_ROLE_VALUES)).first():
            flash("Please choose a valid, active assignee.", "error")
            return redirect(form_url)

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

        if not math.isfinite(quantity) or not math.isfinite(estimated_time) \
                or quantity <= 0 or estimated_time <= 0:
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

        backup_assignee_id, fallback_hours, fallback_error = (
            parse_fallback_fields(assigned_to_id)
        )

        if fallback_error:
            flash(fallback_error, "error")

            return redirect(
                form_url
            )

        is_social_media, social_platforms_csv, social_error = (
            parse_social_media_fields()
        )

        if social_error:
            flash(social_error, "error")

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
            backup_assignee_id=backup_assignee_id,
            fallback_hours=fallback_hours,
            is_social_media=is_social_media,
            social_platforms=social_platforms_csv or None,
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
                    User.role.in_(roles.ALL_ROLE_VALUES),
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

            # Files the upload popup already streamed to the staging
            # prefix while this form was being filled in. The plain
            # <input> above still works for anyone without JavaScript.
            _attach_staged_reference_files(task, uploaded_object_keys)

            created_message = f"Created by {current_user.name}"

            if task.backup_assignee_id and task.fallback_hours:
                backup_name = User.query.get(task.backup_assignee_id).name
                created_message += (
                    f"\nBackup assignee: {backup_name} "
                    f"(shifts after {task.fallback_hours}h if not started)"
                )

            if task.is_social_media and task.social_platforms:
                platform_labels = ", ".join(
                    social.label(key)
                    for key in social.parse_platforms(task.social_platforms)
                )
                created_message += f"\nSocial media: {platform_labels}"

            add_activity(
                task,
                action="created",
                message=created_message,
                old_status=None,
                new_status="Assigned",
            )

            notify_mentioned_users(
                task,
                task.description,
                actor=current_user,
                link=url_for("tasks.task_detail", task_id=task.id),
                source="description",
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
                email=True,
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
        social_platform_options=social.PLATFORMS,
        mention_users=active_user_names(),
    )

@tasks_bp.route("/self-assign", methods=["GET", "POST"])
@login_required
def self_assign_task():

    # See add_task: keeps the drawer flag across validation redirects.
    form_url = url_for("tasks.self_assign_task", **panel_args())

    clients = Client.ordered_with_sub_clients()

    deliverables = ClientDeliverable.query.order_by(
        ClientDeliverable.id.desc()
    ).all()

    if request.method == "POST":

        uploaded_object_keys = []

        deadline = None
        deadline_value = request.form.get("deadline")

        if deadline_value:
            try:
                deadline = datetime.strptime(
                    deadline_value,
                    "%Y-%m-%dT%H:%M"
                )
            except ValueError:
                flash("Deadline format is invalid.", "error")
                return redirect(form_url)

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

        if not math.isfinite(quantity) or not math.isfinite(estimated_time) \
                or quantity <= 0 or estimated_time <= 0:
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

            _attach_staged_reference_files(task, uploaded_object_keys)

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

    clients = Client.ordered_with_sub_clients()

    deliverables = ClientDeliverable.query.order_by(
        ClientDeliverable.id.desc()
    ).all()

    employees = User.query.filter(
        User.status == "active",
        User.role.in_(roles.ALL_ROLE_VALUES)
    ).order_by(
        User.name.asc()
    ).all()

    if request.method == "POST":

        old_status = task.status
        old_assigned_to_id = task.assigned_to_id
        old_backup_assignee_id = task.backup_assignee_id
        old_fallback_hours = task.fallback_hours
        old_is_social_media = task.is_social_media
        old_social_platforms = task.social_platforms or ""

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
            try:
                deadline = datetime.strptime(
                    request.form.get("deadline"),
                    "%Y-%m-%dT%H:%M"
                )
            except ValueError:
                flash("Deadline format is invalid.", "error")
                return redirect(
                    url_for(
                        "tasks.edit_task",
                        task_id=task.id
                    )
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

        # Validate the assignee's role server-side (the change is applied only
        # for managers below, but a tampered POST shouldn't target a
        # non-assignable active user).
        if not User.query.filter(
                User.id == assigned_to_id, User.status == "active",
                User.role.in_(roles.ALL_ROLE_VALUES)).first():
            flash("Please choose a valid, active assignee.", "error")
            return redirect(url_for("tasks.edit_task", task_id=task.id))

        if not math.isfinite(quantity) or not math.isfinite(estimated_time) \
                or quantity <= 0 or estimated_time <= 0:
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

        backup_assignee_id, fallback_hours, fallback_error = (
            parse_fallback_fields(assigned_to_id)
        )

        if fallback_error:
            flash(fallback_error, "error")
            return redirect(
                url_for(
                    "tasks.edit_task",
                    task_id=task.id
                )
            )

        is_social_media, social_platforms_csv, social_error = (
            parse_social_media_fields()
        )

        if social_error:
            flash(social_error, "error")
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
        # Drive the allowed set from hand_moves for EVERYONE (managers too), so
        # Scheduled and Published (drag-locked - they belong to Studio and the
        # approval sign-off) and On Hold / Void (reason-required, set from the
        # task page) are never hand-reachable via the edit dropdown. This closes
        # a back door around the publish gate and the review stages: a
        # manage_tasks holder without publish_tasks could otherwise set a task
        # straight to Published, skipping Core/Client Review entirely.
        can_manage_tasks = has_permission(current_user, "manage_tasks")
        if (
            new_status != task.status
            and new_status not in task_status.hand_moves(
                task.status, can_manage_tasks
            )
        ):
            flash(
                "You can't move this task to that status.",
                "error"
            )
            return redirect(
                url_for(
                    "tasks.edit_task",
                    task_id=task.id
                )
            )

        # A social task publishes through the Studio handoff, never the plain
        # status dropdown - which would skip the publish gate + deliverable
        # count. Block it here before any field is applied.
        if (
            new_status == "Published"
            and new_status != task.status
            and _social_needs_handoff(task)
        ):
            flash(
                "Publish a social task from the task page using “Approve & "
                "Send to Social Studio” or “Mark as manually published”.",
                "error"
            )
            return redirect(url_for("tasks.edit_task", task_id=task.id))

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
        task.priority = new_priority
        task.deadline = deadline
        task.quantity = quantity
        task.estimated_time = estimated_time

        # Reassignment is allowed for task assigners (the `assign_tasks`
        # permission), matching what that permission promises. Client/
        # deliverable retargeting and fallback config stay manager-only. A
        # self-assigned owner with neither permission edits only their own
        # task's content and must not push it onto someone else or move it to
        # another client - can_assign_tasks is false for them (self-assign
        # needs no permission), so the guard below still holds them out.
        if can_assign_tasks(current_user):
            task.assigned_to_id = assigned_to_id
        if can_manage_tasks:
            # Retargeting an already-delivered task has to carry its delivery
            # across, or both service lines are permanently wrong: move a
            # Published task from Reels to Statics and Reels stays +1 while
            # Statics stays -1, with nothing able to reconcile them later.
            # Guarded on completed_at because a task that never counted has
            # nothing to move.
            if task.completed_at and task.deliverable_id != deliverable_id:
                deliverables.move(task.deliverable_id, deliverable_id)

            task.client_id = client_id
            task.deliverable_id = deliverable_id
            fallback_config_changed = (
                old_backup_assignee_id != backup_assignee_id
                or old_fallback_hours != fallback_hours
            )
            task.backup_assignee_id = backup_assignee_id
            task.fallback_hours = fallback_hours
        else:
            fallback_config_changed = False

        if fallback_config_changed:

            # A changed (or newly added / removed) backup config is a
            # fresh decision - re-arm it so a config edited after a
            # previous auto-shift can fire again, and restart the
            # clock so it counts from this edit rather than from
            # whenever the task was originally assigned.
            task.fallback_triggered_at = None

            if task.status == task_status.ASSIGNED:
                task.status_started_at = datetime.utcnow()

        social_config_changed = (
            old_is_social_media != is_social_media
            or old_social_platforms != social_platforms_csv
        )

        task.is_social_media = is_social_media
        task.social_platforms = social_platforms_csv or None

        if social_config_changed:
            # The publish confirmation checklist is keyed off the
            # current platform list - a stale confirmation from before
            # this edit would otherwise let a changed platform slip
            # through unconfirmed the next time this task is published.
            task.social_platforms_published = None

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

            # Only newly-added @mentions get notified - someone already
            # tagged before this edit (e.g. a typo fix elsewhere in the
            # text) shouldn't be re-notified for a mention they already
            # have.
            already_mentioned_ids = {
                u.id for u in find_mentioned_users(old_description)
            }

            notify_mentioned_users(
                task,
                task.description,
                actor=current_user,
                link=url_for("tasks.task_detail", task_id=task.id),
                skip_user_ids=already_mentioned_ids,
                source="description",
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

        if fallback_config_changed:

            old_backup_name = (
                User.query.get(old_backup_assignee_id).name
                if old_backup_assignee_id else "-"
            )

            new_backup_name = (
                User.query.get(backup_assignee_id).name
                if backup_assignee_id else "-"
            )

            changes.append(
                (
                    "Backup Assignee",
                    (
                        f"{old_backup_name} "
                        f"({old_fallback_hours}h)"
                        if old_backup_assignee_id else "-"
                    ),
                    (
                        f"{new_backup_name} "
                        f"({fallback_hours}h)"
                        if backup_assignee_id else "-"
                    ),
                )
            )

        if social_config_changed:

            old_platform_names = ", ".join(
                social.label(key)
                for key in social.parse_platforms(old_social_platforms)
            ) or "-"

            new_platform_names = ", ".join(
                social.label(key)
                for key in social.parse_platforms(social_platforms_csv)
            ) or "-"

            changes.append(
                (
                    "Social Media",
                    old_platform_names if old_is_social_media else "-",
                    new_platform_names if is_social_media else "-",
                )
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
            old_status_for_review = task.status

            if task.timer_started_at and new_status != "In Progress":
                pause_timer(task)

            if new_status == "Published":
                pause_timer(task)

            record_status_time(
                task,
                new_status
            )

            # Same completion side-effects as the Approve button / board drag,
            # so the edit dropdown can't produce a differently-counted task.
            apply_completion_effects(task, new_status)

            # Start the clock when moved into In Progress from the dropdown
            # (the board path does this; the edit path used to leave it dead).
            if new_status == "In Progress" and task.timer_started_at is None:
                # One running task per assignee: pause whichever of theirs is
                # already going, so the edit dropdown can't create two live
                # timers (Start and the board drag both enforce this).
                other_running = Task.query.filter(
                    Task.assigned_to_id == task.assigned_to_id,
                    Task.id != task.id,
                    Task.timer_started_at.isnot(None),
                    Task.status == "In Progress",
                ).first()
                if other_running is not None:
                    pause_timer(other_running)
                    add_activity(
                        other_running, action="auto_paused",
                        message=(f"Auto paused because {current_user.name} "
                                 f"started another task: {task.title}"),
                        old_status="In Progress", new_status="In Progress")
                start_timer(task)

            # Notify reviewers when the edit pushes a task into review.
            if new_status in ("Core Review", "Client Review") \
                    and old_status_for_review not in (
                        "Core Review", "Client Review"):
                _notify_reviewers(task)

        # Visibility is a manager control too - don't let a self-assigned owner
        # rewrite who can see the task.
        if can_manage_tasks:
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
                    User.role.in_(roles.ALL_ROLE_VALUES)
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

        elif task.assigned_to_id and task.assigned_to_id != current_user.id:
            # Don't ping the editor about their own edit (self-assigned owner,
            # or a manager who is also the assignee).
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
        task_status=task_status,
        social_platform_options=social.PLATFORMS,
        task_social_platforms=social.parse_platforms(task.social_platforms),
        mention_users=active_user_names(),
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

    # A given assignee may only have one task running at a time - starting
    # another pauses whichever was already going, keeping that employee's
    # timer state consistent. This holds whoever clicks Start (the assignee
    # or a manager acting for them). (The old non-manager-guarded copy of
    # this query was dead code - immediately overwritten by an identical
    # unconditional one - so runtime behaviour is unchanged; only the dead
    # duplicate is removed.)
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


@tasks_bp.route("/<int:task_id>/reset-to-assigned", methods=["POST"])
@login_required
def reset_to_assigned(task_id):
    """Undo a start: move a task back to Assigned.

    Starting (or pausing) a task should not be a one-way trip. If the
    status was changed by mistake, this sends it back to Assigned -
    banking any time already worked and stopping the timer so nothing
    accrues against a task that is now "not started" again. The move is
    gated by the same transition table as every other status change, so
    it is only permitted where it is actually allowed (In Progress or
    Paused for an assignee), never as a way around the workflow.
    """

    task = Task.query.get_or_404(task_id)
    can_manage = has_permission(current_user, "manage_tasks")

    if not can_manage and task.assigned_to_id != current_user.id:
        flash(
            "You are not allowed to update this task.",
            "error"
        )
        return redirect(url_for("tasks.list_tasks"))

    if not task_status.can_move(task.status, task_status.ASSIGNED, can_manage):
        flash(
            "This task can't be moved back to Assigned from its current status.",
            "error"
        )
        return redirect(
            request.referrer or url_for("tasks.task_detail", task_id=task.id)
        )

    # Stop the clock first (banks worked time), then record the move so
    # the time spent In Progress is filed before the status flips.
    pause_timer(task)

    old_status = record_status_time(task, task_status.ASSIGNED)

    # Back to a clean "not started" state, matching a fresh assignment.
    task.employee_completed = False
    task.employee_completed_at = None

    add_activity(
        task,
        action="reset_to_assigned",
        message=f"Moved back to Assigned by {current_user.name}",
        old_status=old_status,
        new_status=task_status.ASSIGNED
    )

    db.session.commit()

    flash(
        "Task moved back to Assigned.",
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
            f"A {task.status} task cannot be voided.",
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

    # Only In Progress may be submitted to review: an Assigned (never-started)
    # task jumping straight to Core Review is exactly what EMPLOYEE_MOVES
    # forbids, and would enter review with no worked time recorded.
    if task_status.can_move(task.status, "Core Review",
                            has_permission(current_user, "manage_tasks")):

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

        _notify_reviewers(task)

        db.session.commit()

        flash(
            "Task submitted for core review.",
            "success"
        )
    else:
        flash(
            "Start the task before submitting it for review.",
            "error"
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
    reason = (data.get("reason") or "").strip()

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

    # Scheduled and Published are the end of a decision made elsewhere -
    # Studio schedules an approved post, and Published is the client
    # sign-off. Setting either by hand skips that decision; dragging a
    # task out of one quietly undoes it.
    if task_status.is_drag_locked(new_status):
        return jsonify({
            "success": False,
            "message": (
                f"{new_status} is set by the publish flow, not by hand. "
                "Approve the task (or schedule it in Social Studio) instead."
            )
        }), 400

    if task_status.is_drag_locked(task.status):
        return jsonify({
            "success": False,
            "message": (
                f"A {task.status} task cannot be moved from here. "
                "Open the task if it needs to be reopened."
            )
        }), 400

    # Coming back out of a review overturns somebody's decision, so it
    # takes a reason. The UI asks for one before it sends the move;
    # needs_reason tells a caller that skipped the prompt what to do.
    if task_status.needs_reason_to_leave(task.status):

        if len(reason) < task_status.MIN_REASON_LENGTH:
            return jsonify({
                "success": False,
                "needs_reason": True,
                "from_status": task.status,
                "message": (
                    f"Say why this task is leaving {task.status} "
                    "- the assignee will see it on the timeline."
                ),
            }), 400

    # A social task publishes through Social Studio, never a bare board drag -
    # dragging to Published would skip the publish gate entirely.
    if new_status == "Published" and _social_needs_handoff(task):
        return jsonify({
            "success": False,
            "message": (
                "Open the task and use “Approve & Send to Social Studio” "
                "(or “Mark as manually published”) to publish a social task."
            )
        }), 400

    old_status = task.status
    previous_task = None

    # --------------------------------------------
    # Only one In Progress task allowed
    # --------------------------------------------

    if (
        new_status == "In Progress"
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
        "Assigned",
        "Core Review",
        "Client Review",
        "Scheduled",
        "Published",
    ]:

        # Moving back to Assigned (an undo) or forward to a review/scheduled/
        # done state stops the clock: bank whatever was worked and clear the
        # running timer so nothing accrues while the task sits there.
        if task.timer_started_at:

            worked = (
                current_time -
                task.timer_started_at
            ).total_seconds()

            task.worked_seconds = (
                task.worked_seconds or 0
            ) + int(worked)

            task.timer_started_at = None

    # Completion side-effects (employee_completed / completed_at / deliverable
    # count / social_platforms_published) so a board move to a review/done state
    # carries the SAME effects as the Approve button - no more metrics that
    # depend on which control the manager used.
    apply_completion_effects(task, new_status)

    # Reviewers only learn a task is waiting if we tell them - mirror
    # submit_review so a board drag into review notifies them too.
    if new_status in ("Core Review", "Client Review") \
            and old_status not in ("Core Review", "Client Review"):
        _notify_reviewers(task)

    message = (
        f"{current_user.name} moved task from {old_status} to {new_status}."
    )

    # The reason belongs on the timeline, next to the move it explains -
    # that is the only place the assignee will look to find out why their
    # submitted work came back.
    if reason and task_status.needs_reason_to_leave(old_status):
        message = f"{message} Reason: {reason}"

    add_activity(
        task,
        action="status_changed",
        message=message,
        old_status=old_status,
        new_status=new_status,
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
@tasks_bp.route("/assignees")
@login_required
def assignable_users():
    """Active users a task can be assigned to - JSON, for the inline
    assignee picker. Managers only, since only they can reassign."""

    if not can_assign_tasks(current_user):
        return jsonify({"users": []})

    users = User.query.filter(
        User.status == "active",
        User.role.in_(roles.ALL_ROLE_VALUES),
    ).order_by(User.name.asc()).all()

    return jsonify(
        {"users": [{"id": u.id, "name": u.name} for u in users]}
    )


@tasks_bp.route("/<int:task_id>/quick-update", methods=["POST"])
@login_required
def quick_update_task(task_id):
    """Inline single-field edit from the list / board / drawer.

    Changes one of assignee / priority / deadline without opening the full
    edit form. Same permission and notification rules as edit_task, but one
    field at a time and a small JSON reply the tile patches in place.
    """

    task = Task.query.get_or_404(task_id)

    # The door: you must be able to see the task at all. Beyond this the
    # rule is PER FIELD, because they are not the same decision - renaming
    # a task is housekeeping anyone working on it may do, while moving its
    # deadline or its owner is not. Each branch below states its own rule
    # as its first statement; do not lift one back up here.
    if not can_view_task(task):
        return jsonify({"success": False, "message": "Permission denied."}), 403

    # Published / Void are terminal - no quiet edits.
    if task.status in task_status.TERMINAL_STATUSES:
        return jsonify({"success": False, "message": "This task is closed."}), 400

    is_self_assigned_owner = (
        task.created_by_id == current_user.id
        and task.assigned_to_id == current_user.id
    )
    can_manage = has_permission(current_user, "manage_tasks")
    can_edit_fields = can_manage or is_self_assigned_owner

    data = request.get_json(silent=True) or {}
    field = (data.get("field") or "").strip()
    value = data.get("value")

    changes = []

    if field == "title":

        # Anyone who can open the task may rename it. The rule used to be
        # the creator-and-assignee one below, which collapses the moment a
        # task is transferred - so the new owner could not even correct the
        # name of the task they had just been handed.
        if not isinstance(value, str):
            return jsonify({"success": False, "message": "Invalid task name."}), 400

        new_title = value.strip()

        if not new_title:
            return jsonify({"success": False, "message": "Task name cannot be empty."}), 400

        # Task.title is String(255). Refused rather than truncated: a
        # silently shortened title is a corrupted one.
        if len(new_title) > 255:
            return jsonify({
                "success": False,
                "message": "Task name must be 255 characters or fewer.",
            }), 400

        if new_title != task.title:
            changes.append(("Task Name", task.title, new_title))
            task.title = new_title

        display = new_title

    elif field == "priority":

        if not can_edit_fields:
            return jsonify({"success": False, "message": "Permission denied."}), 403

        if value not in ("Low", "Medium", "High", "Urgent"):
            return jsonify({"success": False, "message": "Invalid priority."}), 400

        if value != task.priority:
            changes.append(("Priority", task.priority, value))
            task.priority = value

        display = value

    elif field == "deadline":

        if not can_edit_fields:
            return jsonify({"success": False, "message": "Permission denied."}), 403

        new_deadline = None

        if value:
            try:
                new_deadline = datetime.strptime(value, "%Y-%m-%dT%H:%M")
            except ValueError:
                return jsonify({"success": False, "message": "Invalid date."}), 400

        old = task.deadline.strftime("%d %b %Y %I:%M %p") if task.deadline else "-"
        task.deadline = new_deadline
        new = task.deadline.strftime("%d %b %Y %I:%M %p") if task.deadline else "-"

        if old != new:
            changes.append(("Deadline", old, new))

        display = new

    elif field == "assignee":

        # Reassign is allowed for task assigners (assign_tasks), matching
        # edit_task and what that permission promises - not managers only.
        if not can_assign_tasks(current_user):
            return jsonify({"success": False, "message": "You don't have permission to reassign tasks."}), 403

        assigned_user = User.query.filter_by(id=value, status="active").first()

        if not assigned_user:
            return jsonify({"success": False, "message": "Invalid employee."}), 400

        old_assigned_to_id = task.assigned_to_id

        if assigned_user.id != old_assigned_to_id:

            old_name = task.assigned_to.name if task.assigned_to else "-"
            changes.append(("Assigned To", old_name, assigned_user.name))

            task.assigned_to_id = assigned_user.id
            # A fresh assignee hasn't completed it - mirror edit_task.
            task.employee_completed = False
            task.employee_completed_at = None

            create_notification(
                user_id=assigned_user.id,
                title="Task assigned to you",
                message=f"{current_user.name} assigned you: {task.title}",
                link=url_for("tasks.task_detail", task_id=task.id),
                actor_id=current_user.id,
                task_id=task.id,
            )

            if old_assigned_to_id and old_assigned_to_id != assigned_user.id:
                create_notification(
                    user_id=old_assigned_to_id,
                    title="Task reassigned",
                    message=f"{task.title} is no longer assigned to you.",
                    link=url_for("tasks.task_detail", task_id=task.id),
                    actor_id=current_user.id,
                    task_id=task.id,
                )

        display = assigned_user.name

    else:
        return jsonify({"success": False, "message": "Unknown field."}), 400

    if changes:
        add_activity(
            task,
            action="updated",
            message=build_task_update_message(changes),
        )
        db.session.commit()

    return jsonify({"success": True, "message": "Updated.", "display": display})


@tasks_bp.route("/bulk-update", methods=["POST"])
@login_required
def bulk_update_tasks():
    """Apply one field change to many tasks at once - the manager's
    batch reassign / re-prioritise / re-deadline. Same per-task rules and
    reassignment notifications as the inline quick-edit; closed (Published/
    Void) tasks are skipped rather than failed. Bulk status changes are
    deliberately not offered here - they carry timer and workflow side
    effects that belong on the single-task path.
    """

    # Bulk reassign is open to task assigners (assign_tasks), matching the
    # single-task edit/quick paths; bulk priority/deadline stay manager-only
    # (field-level check below), so assigners gain nothing beyond reassign.
    can_manage = has_permission(current_user, "manage_tasks")
    if not (can_manage or can_assign_tasks(current_user)):
        return jsonify({"success": False, "message": "Permission denied."}), 403

    data = request.get_json(silent=True) or {}
    field = (data.get("field") or "").strip()
    value = data.get("value")
    raw_ids = data.get("task_ids") or []

    if field not in ("assignee", "priority", "deadline"):
        return jsonify({"success": False, "message": "Unsupported field."}), 400

    if field in ("priority", "deadline") and not can_manage:
        return jsonify({"success": False, "message": "Only managers can bulk-change that field."}), 403

    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"success": False, "message": "No tasks selected."}), 400

    # The ids arrive as strings from the checkboxes; the column is an
    # integer, so coerce (and drop anything non-numeric) before querying.
    try:
        task_ids = [int(i) for i in raw_ids]
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid task selection."}), 400

    # Validate the new value once, up front.
    assigned_user = None
    new_deadline = None
    display = value

    if field == "priority":
        if value not in ("Low", "Medium", "High", "Urgent"):
            return jsonify({"success": False, "message": "Invalid priority."}), 400

    elif field == "assignee":
        assigned_user = User.query.filter_by(id=value, status="active").first()
        if not assigned_user:
            return jsonify({"success": False, "message": "Invalid employee."}), 400
        display = assigned_user.name

    elif field == "deadline":
        if value:
            try:
                new_deadline = datetime.strptime(value, "%Y-%m-%dT%H:%M")
            except ValueError:
                return jsonify({"success": False, "message": "Invalid date."}), 400
        display = new_deadline.strftime("%d %b %Y %I:%M %p") if new_deadline else "cleared"

    tasks = Task.query.filter(Task.id.in_(task_ids)).all()

    updated = 0
    skipped = 0

    for task in tasks:

        # Per-task visibility, same gate the single-item quick-edit enforces.
        # The entry guard only requires assign_tasks, which does NOT imply
        # view_all_tasks, so without this a user could bulk-reassign tasks from
        # a client/team they can't even see. Skip (don't error) the rest.
        if not can_view_task(task):
            skipped += 1
            continue

        if task.status in task_status.TERMINAL_STATUSES:
            skipped += 1
            continue

        changed = False

        if field == "priority" and task.priority != value:
            task.priority = value
            changed = True

        elif field == "deadline" and task.deadline != new_deadline:
            task.deadline = new_deadline
            changed = True

        elif field == "assignee" and task.assigned_to_id != assigned_user.id:
            old_id = task.assigned_to_id
            task.assigned_to_id = assigned_user.id
            task.employee_completed = False
            task.employee_completed_at = None

            create_notification(
                user_id=assigned_user.id,
                title="Task assigned to you",
                message=f"{current_user.name} assigned you: {task.title}",
                link=url_for("tasks.task_detail", task_id=task.id),
                actor_id=current_user.id,
                task_id=task.id,
            )
            if old_id and old_id != assigned_user.id:
                create_notification(
                    user_id=old_id,
                    title="Task reassigned",
                    message=f"{task.title} is no longer assigned to you.",
                    link=url_for("tasks.task_detail", task_id=task.id),
                    actor_id=current_user.id,
                    task_id=task.id,
                )
            changed = True

        if changed:
            add_activity(
                task,
                action="updated",
                message=f"{current_user.name} set {field} to {display} (bulk edit).",
            )
            updated += 1
        else:
            skipped += 1

    if updated:
        db.session.commit()

    parts = [f"Updated {updated} task{'' if updated == 1 else 's'}."]
    if skipped:
        parts.append(f"{skipped} unchanged or skipped.")

    return jsonify({
        "success": True,
        "updated": updated,
        "skipped": skipped,
        "message": " ".join(parts),
    })


@tasks_bp.route("/<int:task_id>/approve", methods=["POST"])
@login_required
def approve_task(task_id):
    """Approve whatever gate the task is currently sitting at.

    Two gates, two permissions. Core Review is the craft check - a senior
    editor signing off a junior's cut - and needs `approve_tasks`. Client
    Review is the client-facing sign-off that marks the work delivered,
    and needs `publish_tasks`. They used to be the same permission, which
    meant there was no way to let someone review work without also letting
    them declare it delivered.
    """

    task = Task.query.get_or_404(task_id)

    if task.status == "Client Review":
        allowed = can_publish(current_user)
    else:
        allowed = has_permission(current_user, "approve_tasks") \
            or can_publish(current_user)

    if not allowed:
        flash("You do not have permission to approve at this stage.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

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

        required_platforms = (
            social.parse_platforms(task.social_platforms)
            if task.is_social_media else []
        )

        if required_platforms:

            confirmed_platforms = social.parse_platforms(
                ",".join(request.form.getlist("confirmed_platforms"))
            )

            missing = [
                key for key in required_platforms
                if key not in confirmed_platforms
            ]

            if missing:
                flash(
                    "Confirm this was published on every listed "
                    "platform before publishing: "
                    + ", ".join(social.label(key) for key in missing)
                    + ".",
                    "error"
                )
                return redirect(
                    request.referrer or url_for("tasks.list_tasks")
                )

            task.social_platforms_published = social.format_platforms(
                required_platforms
            )

        old_status = record_status_time(
            task,
            "Published"
        )

        publish_message = f"Published by {current_user.name}"

        if required_platforms:
            publish_message += (
                ". Confirmed live on: "
                + ", ".join(social.label(key) for key in required_platforms)
                + "."
            )

        add_activity(
            task,
            action="published",
            message=publish_message,
            old_status=old_status,
            new_status="Published"
        )

        # Guarded on completed_at, the way apply_completion_effects already
        # was. Without it a double-clicked Approve stamped completed_at twice
        # and counted the delivery twice - the second click arrives while the
        # first request is still in flight, so "the task is already Published"
        # is not yet true anywhere the second request can see.
        if not task.completed_at:
            task.completed_at = ist_now()
            deliverables.adjust_count(task.deliverable_id, +1)

        status_changed = True

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
            task_id=task.id,
            email=True
        )

    db.session.commit()

    return redirect(
        request.referrer or url_for("tasks.list_tasks")
    )


# ======================================================================
# Social Studio handoff (Client Review -> publish)
# ======================================================================

def _social_team_user_ids(exclude_id=None):
    """Active users who can act in Social Studio: both admin roles plus anyone
    explicitly granted manage_social. Used to notify the team when a draft is
    handed off from a task."""
    from app.models import User, UserPermission, Permission
    ids = {
        u.id for u in User.query.filter(
            User.status == "active",
            User.role.in_(roles.MANAGEMENT_ROLES)).all()
    }
    rows = (db.session.query(UserPermission.user_id)
            .join(Permission, Permission.id == UserPermission.permission_id)
            .filter(Permission.code == "manage_social").all())
    ids.update(r[0] for r in rows)
    ids.discard(exclude_id)
    return ids


def _notify_social_team(title, message, link, exclude_id=None):
    for uid in _social_team_user_ids(exclude_id=exclude_id):
        create_notification(user_id=uid, title=title, message=message,
                            link=link, actor_id=exclude_id)


@tasks_bp.route("/<int:task_id>/send-to-social-studio", methods=["POST"])
@login_required
def send_to_social_studio(task_id):
    """Client Review -> hand the approved creative to Social Studio as a draft
    the social team finalizes and publishes. The publish-state then flows back
    onto the task (Scheduled / In publish queue / Published / failed)."""
    if not current_app.config.get("SOCIAL_ENGINE_ENABLED"):
        abort(404)
    # Handing work to Studio is the client-facing step, so it is the
    # publish permission rather than the craft-review one.
    if not can_publish(current_user):
        return redirect(url_for("dashboard.index"))
    task = Task.query.get_or_404(task_id)
    if not task.is_social_media:
        flash("This isn't a social media task.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))
    if task.status != "Client Review":
        flash("Only a task in Client Review can be sent to Social Studio.",
              "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    # Record the client sign-off (same completion flags approve_task sets).
    task.employee_completed = True
    if not task.employee_completed_at:
        task.employee_completed_at = ist_now()

    from app.routes.social import create_draft_from_task
    from app.social.services import task_link
    result = create_draft_from_task(task, actor_id=current_user.id)
    post = result["post"]
    db.session.flush()
    task_link.sync_task_from_posts(task, actor_id=current_user.id)

    add_activity(
        task, action="sent_to_social_studio",
        message=(f"Approved by {current_user.name} and sent to Social Studio "
                 "as a draft to publish."))

    _notify_social_team(
        "New post to publish",
        f"“{task.title}” was approved and sent to Social Studio.",
        link=(url_for("social.edit_post", post_id=post.id)
              if result["n_targets"] else url_for("social.drafts")),
        exclude_id=current_user.id)

    db.session.commit()
    if result["no_channels"]:
        flash("Sent to Social Studio — but this client has no channels "
              "connected yet. Connect them in Social Studio → Accounts, then "
              "open the draft to publish.", "info")
    else:
        flash("Approved and sent to Social Studio. The social team can publish "
              "or schedule it now.", "success")
    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route("/<int:task_id>/mark-manually-published", methods=["POST"])
@login_required
def mark_manually_published(task_id):
    """For a post published directly on the platform, not through Studio. Marks
    the task Published and creates an 'outside Studio' record so the Studio's
    Published list and the task both reflect it."""
    if not current_app.config.get("SOCIAL_ENGINE_ENABLED"):
        abort(404)
    # Declaring something already live is the same sign-off as publishing
    # it from here, so it takes the same permission.
    if not can_publish(current_user):
        return redirect(url_for("dashboard.index"))
    task = Task.query.get_or_404(task_id)
    if task.status not in ("Client Review", "Scheduled"):
        flash("Only a task awaiting publish can be marked manually published.",
              "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    from app.models import SocialPost
    from app.social.services import task_link
    post = SocialPost(
        task_id=task.id, client_id=task.client_id, title=task.title,
        status="published", published_externally=True,
        created_by_id=current_user.id, approved_by_id=current_user.id,
        approved_at=ist_now())
    db.session.add(post)
    db.session.flush()
    task_link.sync_task_from_posts(task, actor_id=current_user.id)
    if task.is_social_media and task.social_platforms:
        task.social_platforms_published = task.social_platforms
    add_activity(
        task, action="published",
        message=(f"Marked manually published by {current_user.name} "
                 "(published directly on the platform, outside Social Studio)."))
    db.session.commit()
    flash("Marked as published (outside Social Studio).", "success")
    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route("/<int:task_id>/retry-publish", methods=["POST"])
@login_required
def retry_task_publish(task_id):
    """Re-queue every failed publish target of this task's Studio post(s)."""
    if not current_app.config.get("SOCIAL_ENGINE_ENABLED"):
        abort(404)
    if not (has_permission(current_user, "approve_tasks")
            or can_publish(current_user)
            or can_use_social(current_user)):
        return redirect(url_for("dashboard.index"))
    task = Task.query.get_or_404(task_id)
    from app.social.services import task_link, recovery, publishing, lifecycle
    n = 0
    for post in task_link.linked_posts(task):
        for t in post.targets:
            # "blocked" as well as "failed" - same reasoning as
            # social.retry_target: a blocked target was refused before sending
            # for a reason the composer can fix, so it is the most retryable
            # state there is, and it was the one this skipped.
            if t.status not in lifecycle.STUCK_TARGET_STATUSES:
                continue
            job = t.job
            if job is not None and recovery.requeue_job(
                    job, actor_id=current_user.id, commit=False):
                pass
            else:
                publishing.publish_target_now(t, actor_id=current_user.id)
            if post.status == "failed":
                post.status = "publishing"
            n += 1
    task_link.sync_task_from_posts(task, actor_id=current_user.id)
    db.session.commit()
    flash(f"Re-queued {n} stuck publish(es) — they'll go out on the next "
          "worker run." if n else "Nothing to retry.",
          "success" if n else "info")
    return redirect(url_for("tasks.task_detail", task_id=task.id))


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

    # Mirror approve_task's per-stage gates: sending a task back from Client
    # Review overturns the client-facing sign-off, so it needs publish rights;
    # Core Review needs approve_tasks (or publish). Previously any approve_tasks
    # holder could reject a Client Review task they weren't allowed to act on.
    if task.status == "Client Review":
        allowed = can_publish(current_user)
    else:
        allowed = has_permission(current_user, "approve_tasks") \
            or can_publish(current_user)
    if not allowed:
        flash("You don't have permission to send this task back at this "
              "stage.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    message = request.form.get(
        "message",
        ""
    ).strip()

    reference_file = request.files.get(
        "reference_file"
    )

    if len(message) < 10:
        flash(
            "Rejection reason must be at least 10 characters - explain what "
            "needs fixing.",
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

        # Store the attachment in R2 like every other upload - not on the
        # app's local disk, which was served straight from /static with no
        # auth and vanished on every restart/redeploy. file_path now holds
        # the R2 object key (content-type is sanitised by StorageService on
        # write); it is served only through the authenticated
        # tasks.task_feedback_file route below.
        object_key = (
            f"feedback/task-{task.id}/{uuid4().hex[:12]}_{safe_name}"
        )

        try:
            StorageService().upload(
                file_obj=reference_file.stream,
                object_key=object_key,
                content_type=reference_file.mimetype,
            )
        except StorageServiceError:
            current_app.logger.exception(
                "Failed to store rejection attachment for task %s.",
                task.id,
            )
            flash(
                "Could not upload the reference file. Please try again.",
                "error",
            )
            return redirect(
                request.referrer or url_for(
                    "tasks.task_detail",
                    task_id=task.id
                )
            )

        file_name = reference_file.filename
        file_path = object_key
        file_type = reference_file.mimetype

    feedback = TaskFeedback(
        task_id=task.id,
        sender_id=current_user.id,
        receiver_id=task.assigned_to_id,
        message=message,
        file_name=file_name,
        file_path=file_path,
        file_type=file_type
    )

    # Bank any running segment before resetting, so worked time already spent
    # isn't discarded when the task goes back for rework.
    pause_timer(task)

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
        task_id=task.id,
        email=True
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


def _task_social_posts(task_id):
    """Social Studio posts created from this task (empty if the engine is off)."""
    if not current_app.config.get("SOCIAL_ENGINE_ENABLED"):
        return []
    from app.models import SocialPost
    return (SocialPost.query.filter_by(task_id=task_id)
            .order_by(SocialPost.created_at.desc()).all())


@tasks_bp.route("/<int:task_id>")
@login_required
def task_detail(task_id):

    task = Task.query.get_or_404(task_id)

    # Checked first. It used to sit below four TaskFile queries, which ran
    # in full for a request that was about to be refused.
    if not can_view_task(task):
        return _task_access_denied(task_id)

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

    # --- Transfer request state -------------------------------------
    # The same rule the POST route enforces, evaluated once here so the
    # button and the route can never disagree about who may transfer.
    pending_transfer = TaskTransferRequest.pending_for(task.id)

    can_request_transfer = _transfer_blocked_reason(task, current_user) is None

    transfer_candidates = []

    if can_request_transfer:
        transfer_candidates = (
            User.query
            .filter(
                User.status == "active",
                User.id != current_user.id,
                User.role.in_(roles.ALL_ROLE_VALUES),
            )
            .order_by(User.name.asc())
            .all()
        )

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
        social=social,
        task_social_platform_keys=social.parse_platforms(task.social_platforms),
        # Social posts spawned from this task (Studio integration).
        social_posts=_task_social_posts(task.id),
        can_manage_tasks=has_permission(current_user, "manage_tasks"),
        current_status_seconds=current_status_seconds,
        current_status=task.status,
        timer_status_label=timer_status_label,
        timedelta=timedelta,
        comments=comments,
        # Names for the comment @-mention autocomplete.
        mention_users=[
            u.name for u in User.query
            .filter_by(status="active")
            .order_by(User.name.asc())
            .all()
        ],
        # Transfer request state for this task: the one live request (if
        # any) plus the people it could be handed to. Both computed here
        # so the template only decides what to show, not who may act.
        pending_transfer=pending_transfer,
        can_request_transfer=can_request_transfer,
        transfer_candidates=transfer_candidates,
        reference_files=reference_files,
        working_files=working_files,
        final_files=final_files,
        submission_files=submission_files,
    )

def _can_view_task_file(task_file):
    """Same rule the preview and download routes apply.

    Every file — reference AND delivered submission — is scoped to the task's
    audience. Submission files used to be readable by anyone (a `folder_type ==
    "submission"` short-circuit), which let any signed-in user enumerate
    /tasks/files/<n>/download and pull every client's delivered creative; the
    task_files_panel listing already hid them from non-scoped users, so the
    direct URLs contradicted it. Gated the same way now."""

    if can_view_all_tasks(current_user):
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

    # ?repair=1 - the grid's own <img> hit a 404 and is asking for the tile
    # to be rebuilt. Only ever set by an onerror handler, because the check
    # behind it is a HEAD against storage and must not run while a page of
    # tiles renders. It verifies before changing anything, so a forged
    # parameter costs one HEAD and nothing else. The reset drops the row to
    # pending, which the branch below then schedules.
    if request.args.get("repair"):
        thumbnails.forget_missing_thumbnail(task_file.id)
        db.session.refresh(task_file)

    if (
        task_file.thumbnail_state == thumbnails.STATE_PENDING
        and thumbnails.supports(task_file)
    ):
        # Don't render inline: a large PDF/PSD would download (up to
        # MAX_DOC_BYTES) and decode inside the request, tying up a worker while
        # a grid fires many tile requests at once. Enqueue it for the
        # background worker and serve the original this once - the tile heals
        # on the next view.
        thumbnails.schedule(task_file.id)

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

    if task_file.thumbnail_state == thumbnails.STATE_READY:
        # The thumbnail for a given file id never changes content, so let
        # the browser keep it rather than re-walking this route for every
        # tile on every visit. Kept under the signed URL's own lifetime.
        response.headers["Cache-Control"] = (
            f"private, max-age={THUMBNAIL_URL_TTL - 60}"
        )
    else:
        # Still being generated, so `url` is the full-size original standing
        # in for one tile. Caching that for an hour would pin the fallback
        # in place long after the real thumbnail existed - and on the repair
        # path it would mean a broken tile healed everywhere except in the
        # browser that asked for the repair.
        response.headers["Cache-Control"] = "no-store"

    return response


@tasks_bp.route("/feedback/<int:feedback_id>/file")
@login_required
def task_feedback_file(feedback_id):
    """Serve a rejection-feedback attachment, access-controlled.

    New attachments live in R2 (file_path = object key) and are served as a
    short-lived presigned redirect. Rows created before this change stored a
    local static path (file_path starts with "uploads/"); those keep working
    via the static handler. Either way, only the reviewer who sent it, the
    assignee who received it, or a manager may open it - closing the old gap
    where these files were readable by anyone who guessed the /static path.
    """
    feedback = TaskFeedback.query.get_or_404(feedback_id)

    if not (
        can_view_all_tasks(current_user)
        or current_user.id in (feedback.sender_id, feedback.receiver_id)
        or (feedback.task and feedback.task.assigned_to_id == current_user.id)
    ):
        abort(403)

    key = feedback.file_path

    if not key:
        abort(404)

    # Legacy local-disk attachment - serve it the old way.
    if key.startswith("uploads/"):
        return redirect(url_for("static", filename=key))

    try:
        url = StorageService().preview_url(object_key=key, expires_in=600)
    except StorageServiceError:
        current_app.logger.exception(
            "Unable to sign rejection attachment for feedback %s.",
            feedback.id,
        )
        abort(404)

    return redirect(url)


@tasks_bp.route("/files/<int:file_id>/preview")
@login_required
def preview_task_file(file_id):

    task_file = TaskFile.query.get_or_404(file_id)
    task = task_file.task

    if not can_view_all_tasks(current_user):

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


@tasks_bp.route("/files/<int:file_id>/ai-check", methods=["POST"])
@login_required
@limiter.limit("60 per hour")
def ai_check_file(file_id):
    """Advisory AI QA pass on a submitted deliverable, on demand. Checks the
    file against the brief and the client's brand knowledge base and returns a
    checklist of findings - never blocks anything. Reviewers or the assignee,
    on a task they can see."""
    from app.ai import settings as ai_settings, usage as ai_usage
    if not ai_settings.feature_enabled("qa"):
        return jsonify(error="AI assist is not available."), 503
    if not ai_usage.within_budget():
        return jsonify(error="Monthly AI budget reached — raise it in AI Settings."), 503

    task_file = TaskFile.query.get_or_404(file_id)
    task = task_file.task

    if not (can_view_task(task)
            and (can_review(current_user)
                 or task.assigned_to_id == current_user.id)):
        return jsonify(error="You can't check this file."), 403

    from app.ai import service as ai_service
    from app.ai.errors import AIAuth, AIDisabled, AITransient

    try:
        result = ai_service.check_media(
            task_file, created_by_id=current_user.id)
    except AIDisabled:
        return jsonify(error="AI assist is not available."), 503
    except AITransient:
        return jsonify(error="The AI service is busy — try again shortly."), 503
    except AIAuth:
        current_app.logger.error("[ai] provider auth failed")
        return jsonify(error="AI is misconfigured — contact an admin."), 502
    except Exception:  # noqa: BLE001 - never surface internals / keys
        current_app.logger.exception("[ai] media check failed")
        return jsonify(error="Couldn't check that file — please try again."), 502
    return jsonify(**result)


@tasks_bp.route("/files/<int:file_id>/download")
@login_required
def download_task_file(file_id):

    task_file = TaskFile.query.get_or_404(file_id)
    task = task_file.task

    if not can_view_all_tasks(current_user):

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

    # A file pulled into a Social Studio post is referenced by a
    # SocialMediaAsset (FK, no cascade), so deleting it would fail with a bare
    # IntegrityError. Detect it and explain, instead of "please try again".
    if current_app.config.get("SOCIAL_ENGINE_ENABLED"):
        from app.models import SocialMediaAsset
        if SocialMediaAsset.query.filter_by(task_file_id=task_file.id).first():
            flash(
                "This file is used in a Social Studio post — remove it from "
                "the post (or delete the post) before deleting the file.",
                "error")
            return redirect(url_for("tasks.task_detail", task_id=task.id))

    filename = task_file.original_filename
    object_key = task_file.object_key
    thumbnail_key = task_file.thumbnail_key

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

    # Remove the original and its derived thumbnail together, so deleting a
    # file reclaims all of its storage and never orphans a thumbnail in R2.
    for leftover_key, label in ((object_key, "storage object"), (thumbnail_key, "thumbnail")):
        if not leftover_key:
            continue
        try:
            StorageService().delete(object_key=leftover_key)
        except Exception:
            current_app.logger.exception(
                "Unable to remove %s for deleted task file %s: %s",
                label, file_id, leftover_key,
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


#: Where a reference file lives between being chosen and the task
#: existing. Reference files are picked on the CREATE form, and
#: StorageService.upload_task_file refuses without a saved task - so they
#: are staged here first and attached once the task has an id. Anything
#: left unattached is swept by the media GC.
REFERENCE_STAGING_PREFIX = "task_staging/"


@tasks_bp.route("/reference/stage", methods=["POST"])
@login_required
def stage_reference_file():
    """Hold one reference file until the task it belongs to exists.

    No task to authorise against yet, so this is login-only. The key is a
    uuid the caller never chooses, and every other route that touches
    these objects checks the prefix - so holding one grants nothing
    beyond the file you just uploaded.
    """
    import os
    import re as _re
    from uuid import uuid4

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify(success=False, message="No file provided."), 400

    safe = _re.sub(
        r"[^A-Za-z0-9._-]", "_", os.path.basename(uploaded.filename)
    )[:80] or "file"
    object_key = f"{REFERENCE_STAGING_PREFIX}{uuid4().hex}_{safe}"

    try:
        # Streamed, not read into memory: reference files can be video,
        # and a few concurrent uploads would otherwise sit in the worker.
        result = StorageService().upload(
            file_obj=uploaded.stream,
            object_key=object_key,
            content_type=uploaded.mimetype,
        )
    except Exception:  # noqa: BLE001
        current_app.logger.exception("Reference staging failed.")
        return jsonify(
            success=False,
            message="Upload failed — please try again.",
        ), 500

    return jsonify(
        success=True,
        object_key=object_key,
        original_filename=uploaded.filename,
        mime_type=result.get("content_type") or uploaded.mimetype,
        file_size=result.get("content_length") or 0,
        bucket_name=result.get("bucket_name"),
    )


@tasks_bp.route("/reference/discard", methods=["POST"])
@login_required
def discard_reference_file():
    """Drop a staged reference file - the × , or closing the popup."""
    data = request.get_json(silent=True) or {}
    object_key = (data.get("object_key") or "").strip()

    # Prefix check is the whole guard: it confines this to the staging
    # area, so a tampered key cannot point at a task's real files.
    if not object_key.startswith(REFERENCE_STAGING_PREFIX):
        return jsonify(success=False, message="Not a staged file."), 400

    try:
        StorageService().delete(object_key=object_key)
    except Exception:  # noqa: BLE001 - the GC sweep is the backstop
        current_app.logger.warning(
            "Could not discard staged reference %s", object_key)

    return jsonify(success=True)


def _attach_staged_reference_files(task, uploaded_object_keys=None):
    """Turn the staged objects the form carried into TaskFile rows.

    No copy and no re-upload: TaskFile stores object_key, so attaching is
    just a row pointing at the object already in storage. The thumbnail
    session-event picks the new rows up exactly as it does for a direct
    upload.
    """
    import json as _json

    raw = request.form.get("staged_reference_files")
    if not raw:
        return 0

    try:
        staged = _json.loads(raw)
    except (ValueError, TypeError):
        current_app.logger.warning("Ignoring unreadable staged reference list")
        return 0

    if not isinstance(staged, list):
        return 0

    # NOT NULL, and the value in the payload came from the browser - so a
    # truncated or hand-edited list must not be able to fail the INSERT and
    # take the whole task creation down with it.
    default_bucket = current_app.config.get("R2_BUCKET_NAME")

    attached = 0
    for item in staged:
        if not isinstance(item, dict):
            continue
        object_key = (item.get("object_key") or "").strip()
        if not object_key.startswith(REFERENCE_STAGING_PREFIX):
            continue

        db.session.add(TaskFile(
            task_id=task.id,
            bucket_name=item.get("bucket_name") or default_bucket,
            storage_provider="r2",
            object_key=object_key,
            original_filename=(item.get("original_filename") or "file")[:255],
            stored_filename=object_key.rsplit("/", 1)[-1],
            mime_type=item.get("mime_type"),
            file_size=item.get("file_size") or 0,
            folder_type="reference",
            version=1,
            is_final=False,
            uploaded_by_id=current_user.id,
        ))
        attached += 1
        if uploaded_object_keys is not None:
            uploaded_object_keys.append(object_key)

    return attached


def _require_submission_uploader(task):

    if task.assigned_to_id != current_user.id:
        return jsonify(
            success=False,
            message="Only the assigned employee can upload submission files.",
        ), 403

    return None


@tasks_bp.route(
    "/<int:task_id>/submission/discard/<int:file_id>",
    methods=["POST"],
)
@login_required
def discard_submission_file(task_id, file_id):
    """Undo one upload that already finished.

    The × on a completed row, and every row when the popup is closed.
    "Cancel" has to mean cancel: an aborted transfer leaves nothing
    behind, so a finished one must not either.

    Narrow on purpose - a submission file, on this task, uploaded by the
    caller, and not yet announced. Deleting someone else's work, or a file
    the reviewer has already been told about, goes through the ordinary
    delete route with its own permissions and activity trail.
    """
    task = Task.query.get_or_404(task_id)

    permission_error = _require_submission_uploader(task)
    if permission_error:
        return permission_error

    task_file = TaskFile.query.filter_by(
        id=file_id,
        task_id=task.id,
        folder_type="submission",
        uploaded_by_id=current_user.id,
    ).first()

    if task_file is None:
        return jsonify(
            success=False,
            message="That file is not one of yours to discard.",
        ), 404

    object_key = task_file.object_key
    thumbnail_key = task_file.thumbnail_key

    try:
        db.session.delete(task_file)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Unable to discard submission file %s.", file_id)
        return jsonify(
            success=False,
            message="Could not discard the file.",
        ), 500

    # Storage after the row: an orphan object costs pennies and is swept
    # up later, whereas a row pointing at a deleted object is a broken
    # file in the UI.
    storage = StorageService()
    for key in (object_key, thumbnail_key):
        if not key:
            continue
        try:
            storage.delete(object_key=key)
        except Exception:  # noqa: BLE001 - the row is already gone
            current_app.logger.warning(
                "Discarded file %s but could not remove %s", file_id, key)

    return jsonify(success=True)


@tasks_bp.route(
    "/<int:task_id>/submission/commit",
    methods=["POST"],
)
@login_required
def commit_submission_upload(task_id):
    """Announce a finished batch: one activity entry, one notification.

    The uploads themselves are silent (see
    complete_submission_multipart_upload), so this is the moment the work
    becomes visible to anyone else - which is exactly why it is a
    deliberate press of Done rather than a side effect of the last file
    landing.
    """
    task = Task.query.get_or_404(task_id)

    permission_error = _require_submission_uploader(task)
    if permission_error:
        return permission_error

    data = request.get_json(silent=True) or {}
    file_ids = [i for i in (data.get("file_ids") or []) if isinstance(i, int)]

    files = TaskFile.query.filter(
        TaskFile.id.in_(file_ids or [-1]),
        TaskFile.task_id == task.id,
        TaskFile.folder_type == "submission",
        TaskFile.uploaded_by_id == current_user.id,
    ).all() if file_ids else []

    if not files:
        return jsonify(
            success=False,
            message="Nothing to submit.",
        ), 400

    count = len(files)

    try:
        add_activity(
            task,
            action="submission_uploaded",
            message=(
                f"{current_user.name} uploaded "
                f"{count} submission file(s)."
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
                link=url_for("tasks.task_detail", task_id=task.id),
                actor_id=current_user.id,
                task_id=task.id,
            )

        db.session.commit()

    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Unable to commit submission upload for task %s.", task.id)
        return jsonify(
            success=False,
            message="The files uploaded, but recording them failed.",
        ), 500

    return jsonify(success=True, count=count)


def _submission_key_belongs_to(task, object_key):
    """Does this multipart key live under this task's own submission prefix?

    The same test the storage layer already applies when a multipart upload is
    COMPLETED (StorageService.complete_task_file_multipart_upload), which is
    what stops a tampered client registering a TaskFile row against an
    arbitrary object. The part-url and abort routes took object_key straight
    out of the JSON body with no such check, so a signed-in user could mint
    part URLs against - or abort - another task's in-flight upload just by
    naming its key.

    S3 semantics contain it in practice: a live multipart upload has to exist
    for that exact key and upload_id. That makes it latent rather than live,
    but the containment is a property of the object store, not of anything
    this application controls, and it costs one comparison to not depend on it.
    """
    expected = "/TASK-%s/submission/" % (getattr(task, "task_code", "") or "")
    key = str(object_key or "")
    return key.startswith("clients/") and expected in key


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

    if not _submission_key_belongs_to(task, object_key):
        return jsonify(
            success=False,
            message="object_key does not match this task's storage location.",
        ), 400

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

        # Deliberately silent: no activity entry, no notification. One
        # upload is one file, and a person sending five files should not
        # fire five notifications at whoever set the task. The batch is
        # announced once by commit_submission_upload, after they press
        # Done - which is also what makes cancelling the whole popup
        # possible without having already told anyone.
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
        # Flat too: the popup keeps this to name the file in its commit
        # and discard calls, and reads better than digging into `file`.
        file_id=task_file.id,
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

    if not _submission_key_belongs_to(task, object_key):
        return jsonify(
            success=False,
            message="object_key does not match this task's storage location.",
        ), 400

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

    if not can_view_task(task):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "success": False,
                "message": "You do not have access to this task.",
            }), 403
        flash("You do not have access to this task.", "error")
        return redirect(url_for("tasks.list_tasks"))

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

    comment_link = url_for("tasks.task_detail", task_id=task.id)

    # Tagged teammates get a "mentioned you" notification first; whoever
    # is mentioned is skipped below so they don't also get the generic one.
    mentioned = notify_mentioned_users(
        task, message, current_user, comment_link
    )
    mentioned_ids = {u.id for u in mentioned}

    if task.assigned_to_id != current_user.id and task.assigned_to_id not in mentioned_ids:
        create_notification(
            user_id=task.assigned_to_id,
            title="New Comment",
            message=f"{current_user.name} commented on '{task.title}'",
            link=comment_link,
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

    if not can_view_task(task):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "success": False,
                "message": "You do not have access to this task.",
            }), 403
        flash("You do not have access to this task.", "error")
        return redirect(url_for("tasks.list_tasks"))

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

    reply_link = url_for("tasks.task_detail", task_id=task.id)

    mentioned = notify_mentioned_users(
        task, message, current_user, reply_link
    )
    mentioned_ids = {u.id for u in mentioned}

    if comment.user_id != current_user.id and comment.user_id not in mentioned_ids:
        create_notification(
            user_id=comment.user_id,
            title="New Reply",
            message=f"{current_user.name} replied to your comment.",
            link=reply_link,
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


# ===========================
# Task transfer requests
#
# An employee handing their task to a teammate. Peer-to-peer: the
# recipient accepting is what moves the task - no manager sign-off,
# since managers can already reassign directly. They are notified when
# a transfer lands so a task changing hands is never invisible to
# whoever owns delivery.
# ===========================

def _transfer_blocked_reason(task, requester):
    """Why `requester` may not open a transfer on `task`, or None.

    Returned as a message rather than a bool so the caller can say what
    is wrong instead of a blanket "not allowed".
    """

    if task.assigned_to_id != requester.id:
        return "Only the person a task is assigned to can transfer it."

    if task.status in task_status.TERMINAL_STATUSES:
        return f"A {task.status} task cannot be transferred."

    if TaskTransferRequest.pending_for(task.id):
        return "This task already has a transfer request awaiting a reply."

    return None


@tasks_bp.route("/<int:task_id>/transfer/request", methods=["POST"])
@login_required
def request_task_transfer(task_id):

    task = Task.query.get_or_404(task_id)

    blocked = _transfer_blocked_reason(task, current_user)

    if blocked:
        flash(blocked, "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    to_user_raw = (request.form.get("to_user_id") or "").strip()

    if not to_user_raw.isdigit():
        flash("Choose a teammate to transfer this task to.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    to_user_id = int(to_user_raw)

    if to_user_id == current_user.id:
        flash("You cannot transfer a task to yourself.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    to_user = User.query.filter_by(id=to_user_id, status="active").first()

    if not to_user:
        flash("That teammate is not available.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    message = (request.form.get("message") or "").strip() or None

    transfer = TaskTransferRequest(
        task_id=task.id,
        from_user_id=current_user.id,
        to_user_id=to_user.id,
        message=message,
    )
    db.session.add(transfer)

    activity_message = (
        f"{current_user.name} asked {to_user.name} to take over this task."
    )

    if message:
        activity_message += f"\nReason: {message}"

    add_activity(
        task,
        action="transfer_requested",
        message=activity_message,
    )

    notify_message = (
        f"{current_user.name} wants to transfer '{task.title}' to you."
    )

    if message:
        notify_message += f" Reason: {message}"

    create_notification(
        user_id=to_user.id,
        title="Task transfer request",
        message=notify_message,
        link=url_for("tasks.task_detail", task_id=task.id),
        actor_id=current_user.id,
        task_id=task.id,
        email=True,
    )

    db.session.commit()

    flash(f"Transfer request sent to {to_user.name}.", "success")

    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route("/transfer/<int:transfer_id>/accept", methods=["POST"])
@login_required
def accept_task_transfer(transfer_id):

    transfer = TaskTransferRequest.query.get_or_404(transfer_id)
    task = transfer.task

    if transfer.to_user_id != current_user.id:
        flash("Only the person asked can accept this transfer.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    if not transfer.is_pending:
        flash("This transfer request has already been answered.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    # Re-validated here, not just at request time: the task may have been
    # reassigned by a manager, or closed, in the meantime - in which case
    # honouring the old request would silently undo that.
    if task.assigned_to_id != transfer.from_user_id:
        transfer.status = TaskTransferRequest.CANCELLED
        transfer.responded_at = datetime.utcnow()
        db.session.commit()

        flash(
            "This task was reassigned since the request was sent, so the "
            "transfer no longer applies.",
            "error",
        )
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    if task.status in task_status.TERMINAL_STATUSES:
        transfer.status = TaskTransferRequest.CANCELLED
        transfer.responded_at = datetime.utcnow()
        db.session.commit()

        flash(
            f"This task is {task.status} and can no longer be transferred.",
            "error",
        )
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    previous_assignee = task.assigned_to
    previous_name = previous_assignee.name if previous_assignee else "the previous assignee"

    # Bank whatever the outgoing assignee spent in the current status, and
    # stop a running timer, so their time is recorded against them and the
    # new assignee starts from zero. Same treatment the automatic
    # backup-assignee shift applies (see services/task_fallback.py).
    pause_timer(task)
    record_status_time(task, task.status)

    task.assigned_to_id = current_user.id
    # Only clear the completion flags if the task is BEFORE review. A task
    # transferred while in Core/Client Review or Scheduled is already
    # "employee complete"; clearing the flag would silently drop it from
    # completed-throughput metrics while its status stays in review.
    if task.status not in task_status.REVIEW_STATUSES \
            and task.status != task_status.SCHEDULED:
        task.employee_completed = False
        task.employee_completed_at = None

    transfer.status = TaskTransferRequest.ACCEPTED
    transfer.responded_at = datetime.utcnow()

    add_activity(
        task,
        action="transfer_accepted",
        message=f"{current_user.name} accepted the transfer from {previous_name}.",
    )

    link = url_for("tasks.task_detail", task_id=task.id)

    create_notification(
        user_id=transfer.from_user_id,
        title="Transfer accepted",
        message=f"{current_user.name} took over '{task.title}'.",
        link=link,
        actor_id=current_user.id,
        task_id=task.id,
        email=True,
    )

    # Whoever owns delivery should know the task changed hands, even
    # though their approval was not required.
    watcher_id = task.created_by_id

    if watcher_id and watcher_id not in (transfer.from_user_id, current_user.id):
        create_notification(
            user_id=watcher_id,
            title="Task transferred",
            message=(
                f"'{task.title}' moved from {previous_name} "
                f"to {current_user.name}."
            ),
            link=link,
            actor_id=current_user.id,
            task_id=task.id,
        )

    db.session.commit()

    flash(f"You have taken over '{task.title}'.", "success")

    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route("/transfer/<int:transfer_id>/decline", methods=["POST"])
@login_required
def decline_task_transfer(transfer_id):

    transfer = TaskTransferRequest.query.get_or_404(transfer_id)
    task = transfer.task

    if transfer.to_user_id != current_user.id:
        flash("Only the person asked can decline this transfer.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    if not transfer.is_pending:
        flash("This transfer request has already been answered.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    reason = (request.form.get("response_message") or "").strip() or None

    transfer.status = TaskTransferRequest.REJECTED
    transfer.response_message = reason
    transfer.responded_at = datetime.utcnow()

    activity_message = f"{current_user.name} declined the transfer."

    if reason:
        activity_message += f"\nReason: {reason}"

    add_activity(
        task,
        action="transfer_declined",
        message=activity_message,
    )

    notify_message = f"{current_user.name} declined to take '{task.title}'."

    if reason:
        notify_message += f" Reason: {reason}"

    create_notification(
        user_id=transfer.from_user_id,
        title="Transfer declined",
        message=notify_message,
        link=url_for("tasks.task_detail", task_id=task.id),
        actor_id=current_user.id,
        task_id=task.id,
        email=True,
    )

    db.session.commit()

    flash("Transfer request declined.", "success")

    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route("/transfer/<int:transfer_id>/cancel", methods=["POST"])
@login_required
def cancel_task_transfer(transfer_id):

    transfer = TaskTransferRequest.query.get_or_404(transfer_id)
    task = transfer.task

    # The requester withdrawing, or a manager clearing a request that is
    # blocking their own reassignment.
    if not (
        transfer.from_user_id == current_user.id
        or has_permission(current_user, "manage_tasks")
    ):
        flash("You cannot cancel this transfer request.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    if not transfer.is_pending:
        flash("This transfer request has already been answered.", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    transfer.status = TaskTransferRequest.CANCELLED
    transfer.responded_at = datetime.utcnow()

    add_activity(
        task,
        action="transfer_cancelled",
        message=f"{current_user.name} cancelled the transfer request.",
    )

    if transfer.to_user_id != current_user.id:
        create_notification(
            user_id=transfer.to_user_id,
            title="Transfer request withdrawn",
            message=f"The request to transfer '{task.title}' to you was cancelled.",
            link=url_for("tasks.task_detail", task_id=task.id),
            actor_id=current_user.id,
            task_id=task.id,
        )

    db.session.commit()

    flash("Transfer request cancelled.", "success")

    return redirect(url_for("tasks.task_detail", task_id=task.id))
