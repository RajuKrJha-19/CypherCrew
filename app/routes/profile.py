"""Self-service profile: every signed-in user can view and edit their own
profile and set a picture, regardless of role. Viewing *other* people's
profiles is handled by users.user_detail (management-scoped).
"""

import os
import uuid
from datetime import datetime

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)
from flask_login import login_required, current_user

from app.extensions import db
from app.storage.storage_service import StorageService, StorageServiceError

profile_bp = Blueprint("profile", __name__)

#: Image types we accept for an avatar. Kept to formats browsers render
#: inline and R2 stores as-is (see StorageService content-type policy).
ALLOWED_AVATAR_EXT = {"png", "jpg", "jpeg", "gif", "webp"}

#: Hard cap so a huge upload can't tie up the worker or bloat storage.
MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 5 MB


@profile_bp.route("/profile")
@login_required
def my_profile():
    return render_template(
        "users/profile.html",
        user=current_user,
        is_self=True,
    )


@profile_bp.route("/profile/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    user = current_user

    if request.method == "POST":

        name = request.form.get("name", "").strip()

        # name is NOT NULL and is the person's display name everywhere.
        if not name:
            flash("Name is required.", "error")
            return redirect(url_for("profile.edit_profile"))

        dob = None
        dob_raw = request.form.get("date_of_birth", "").strip()

        if dob_raw:
            try:
                dob = datetime.strptime(dob_raw, "%Y-%m-%d").date()
            except ValueError:
                flash("Date of birth must be a valid date.", "error")
                return redirect(url_for("profile.edit_profile"))

        user.name = name
        user.phone = request.form.get("phone", "").strip()
        user.designation = request.form.get("designation", "").strip()
        user.department = request.form.get("department", "").strip()
        user.location = request.form.get("location", "").strip()
        user.bio = request.form.get("bio", "").strip()
        user.date_of_birth = dob

        db.session.commit()

        flash("Profile updated.", "success")
        return redirect(url_for("profile.my_profile"))

    return render_template("users/profile_edit.html", user=user)


@profile_bp.route("/profile/avatar", methods=["POST"])
@login_required
def upload_avatar():
    user = current_user
    uploaded = request.files.get("avatar")

    if not uploaded or not (uploaded.filename or "").strip():
        flash("Please choose an image to upload.", "error")
        return redirect(url_for("profile.edit_profile"))

    ext = uploaded.filename.rsplit(".", 1)[-1].lower() if "." in uploaded.filename else ""

    if ext not in ALLOWED_AVATAR_EXT:
        flash("Profile picture must be a PNG, JPG, GIF or WEBP image.", "error")
        return redirect(url_for("profile.edit_profile"))

    # Measure the stream without loading it all into memory twice.
    uploaded.stream.seek(0, os.SEEK_END)
    size = uploaded.stream.tell()
    uploaded.stream.seek(0)

    if size == 0:
        flash("That image appears to be empty.", "error")
        return redirect(url_for("profile.edit_profile"))

    if size > MAX_AVATAR_BYTES:
        flash("Profile picture must be 5 MB or smaller.", "error")
        return redirect(url_for("profile.edit_profile"))

    storage = StorageService()
    object_key = f"avatars/user_{user.id}/{uuid.uuid4().hex}.{ext}"
    previous_key = user.avatar_key

    try:
        storage.upload(
            file_obj=uploaded.stream,
            object_key=object_key,
            content_type=uploaded.content_type,
        )
    except StorageServiceError:
        flash("Could not upload the image. Please try again.", "error")
        return redirect(url_for("profile.edit_profile"))

    user.avatar_key = object_key
    db.session.commit()

    # Best-effort cleanup of the old picture; never fail the request for it.
    if previous_key:
        try:
            storage.delete(object_key=previous_key)
        except Exception:
            pass

    flash("Profile picture updated.", "success")
    return redirect(url_for("profile.my_profile"))


@profile_bp.route("/profile/avatar/remove", methods=["POST"])
@login_required
def remove_avatar():
    user = current_user
    previous_key = user.avatar_key

    user.avatar_key = None
    db.session.commit()

    if previous_key:
        try:
            StorageService().delete(object_key=previous_key)
        except Exception:
            pass

    flash("Profile picture removed.", "success")
    return redirect(url_for("profile.my_profile"))
