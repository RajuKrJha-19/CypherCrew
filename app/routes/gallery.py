from datetime import timedelta

from flask import Blueprint, render_template, request
from flask_login import login_required

from sqlalchemy import or_

from app.models import TaskFile, User


gallery_bp = Blueprint(
    "gallery",
    __name__,
    url_prefix="/gallery"
)


@gallery_bp.route("/")
@login_required
def index():

    search = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = 40

    query = (
        TaskFile.query
        .join(User, User.id == TaskFile.uploaded_by_id)
        .filter(TaskFile.folder_type.in_(["reference", "submission"]))
    )

    if search:

        like_pattern = f"%{search}%"

        query = query.filter(
            or_(
                TaskFile.original_filename.ilike(like_pattern),
                User.name.ilike(like_pattern)
            )
        )

    query = query.order_by(TaskFile.created_at.desc())

    pagination = query.paginate(
        page=page,
        per_page=per_page,
        error_out=False
    )

    files = pagination.items

    grouped_files = []
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

    return render_template(
        "gallery/index.html",
        grouped_files=grouped_files,
        pagination=pagination,
        search=search,
        timedelta=timedelta
    )