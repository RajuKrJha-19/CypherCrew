from datetime import timedelta

from flask import Blueprint, render_template, request
from flask_login import login_required

from sqlalchemy import or_, cast, String

from app.models import TaskFile, User, Task, Client


gallery_bp = Blueprint(
    "gallery",
    __name__,
    url_prefix="/gallery"
)


#: Sort options offered in the toolbar. Kept here rather than in the
#: template so the label, the query-string value and the ordering can
#: never drift apart.
SORT_OPTIONS = [
    ("newest", "Newest first"),
    ("oldest", "Oldest first"),
    ("name", "Name (A-Z)"),
    ("name_desc", "Name (Z-A)"),
    ("largest", "Largest first"),
    ("smallest", "Smallest first"),
]

DEFAULT_SORT = "newest"

#: Grouping by upload date only tells you anything while the list is
#: ordered by date. Sorted by name or size the headings would just chop
#: the order into arbitrary pieces, so the grid goes flat instead.
DATE_GROUPED_SORTS = {"newest", "oldest"}

TYPE_OPTIONS = [
    ("image", "Images"),
    ("video", "Videos"),
    ("document", "Documents"),
]

FOLDER_OPTIONS = [
    ("reference", "Reference files"),
    ("submission", "Submissions"),
]


def _apply_type_filter(query, file_type):
    """Filter on the broad kind of file, not an exact mime type."""

    if file_type == "image":
        return query.filter(TaskFile.mime_type.ilike("image/%"))

    if file_type == "video":
        return query.filter(TaskFile.mime_type.ilike("video/%"))

    if file_type == "document":
        # Everything that is not a picture or a clip - PDFs, sheets,
        # archives, and rows with no recorded mime type at all.
        return query.filter(
            or_(
                TaskFile.mime_type.is_(None),
                ~TaskFile.mime_type.ilike("image/%")
                & ~TaskFile.mime_type.ilike("video/%"),
            )
        )

    return query


def _apply_sort(query, sort):

    if sort == "oldest":
        return query.order_by(TaskFile.created_at.asc())

    if sort == "name":
        return query.order_by(TaskFile.original_filename.asc())

    if sort == "name_desc":
        return query.order_by(TaskFile.original_filename.desc())

    if sort == "largest":
        return query.order_by(TaskFile.file_size.desc().nullslast())

    if sort == "smallest":
        return query.order_by(TaskFile.file_size.asc().nullsfirst())

    return query.order_by(TaskFile.created_at.desc())


@gallery_bp.route("/")
@login_required
def index():

    search = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)

    sort = request.args.get("sort", "").strip() or DEFAULT_SORT

    if sort not in dict(SORT_OPTIONS):
        sort = DEFAULT_SORT

    file_type = request.args.get("type", "").strip()

    if file_type not in dict(TYPE_OPTIONS):
        file_type = ""

    folder = request.args.get("folder", "").strip()

    if folder not in dict(FOLDER_OPTIONS):
        folder = ""

    # isdigit() rather than type=int: a non-numeric value should mean
    # "no filter", not a 500 and not a silent filter on 0.
    uploader_raw = request.args.get("uploader", "").strip()
    uploader_id = int(uploader_raw) if uploader_raw.isdigit() else None

    client_raw = request.args.get("client", "").strip()
    client_id = int(client_raw) if client_raw.isdigit() else None

    per_page = 40

    query = (
        TaskFile.query
        .join(User, User.id == TaskFile.uploaded_by_id)
        .join(Task, Task.id == TaskFile.task_id)
        .filter(TaskFile.folder_type.in_(["reference", "submission"]))
    )

    if search:

        like_pattern = f"%{search}%"

        # Widened beyond filename/uploader: people look for "the file on
        # the Hope Plus reel", which is the task or the client, not a
        # filename they never chose.
        query = query.filter(
            or_(
                TaskFile.original_filename.ilike(like_pattern),
                User.name.ilike(like_pattern),
                Task.title.ilike(like_pattern),
                # task_code is an Integer column; ilike() against it is a
                # type error in PostgreSQL. Cast first, the same way the
                # task list's own search does.
                cast(Task.task_code, String).ilike(like_pattern),
            )
        )

    query = _apply_type_filter(query, file_type)

    if folder:
        query = query.filter(TaskFile.folder_type == folder)

    if uploader_id:
        query = query.filter(TaskFile.uploaded_by_id == uploader_id)

    if client_id:
        query = query.filter(Task.client_id == client_id)

    query = _apply_sort(query, sort)

    pagination = query.paginate(
        page=page,
        per_page=per_page,
        error_out=False
    )

    files = pagination.items

    grouped_files = []

    if sort in DATE_GROUPED_SORTS:

        current_date_label = None
        current_group = None

        for task_file in files:

            file_date_label = (
                task_file.created_at + timedelta(hours=5, minutes=30)
            ).strftime("%d %b %Y")

            if file_date_label != current_date_label:
                current_date_label = file_date_label
                current_group = {
                    "date_label": file_date_label,
                    "files": []
                }
                grouped_files.append(current_group)

            current_group["files"].append(task_file)

    elif files:
        grouped_files = [{"date_label": None, "files": files}]

    # Only offer people and clients that actually have files here, so a
    # dropdown can't lead to a guaranteed empty result.
    uploaders = (
        User.query
        .join(TaskFile, TaskFile.uploaded_by_id == User.id)
        .filter(TaskFile.folder_type.in_(["reference", "submission"]))
        .distinct()
        .order_by(User.name.asc())
        .all()
    )

    clients = (
        Client.query
        .join(Task, Task.client_id == Client.id)
        .join(TaskFile, TaskFile.task_id == Task.id)
        .filter(TaskFile.folder_type.in_(["reference", "submission"]))
        .distinct()
        .order_by(Client.client_name.asc())
        .all()
    )

    active_filters = sum(
        1 for value in (file_type, folder, uploader_raw, client_raw) if value
    )

    return render_template(
        "gallery/index.html",
        grouped_files=grouped_files,
        pagination=pagination,
        search=search,
        sort=sort,
        sort_options=SORT_OPTIONS,
        sort_label=dict(SORT_OPTIONS)[sort],
        file_type=file_type,
        type_options=TYPE_OPTIONS,
        folder=folder,
        folder_options=FOLDER_OPTIONS,
        uploader_id=uploader_id,
        uploaders=uploaders,
        client_id=client_id,
        clients=clients,
        active_filters=active_filters,
        total_files=pagination.total,
        timedelta=timedelta
    )
