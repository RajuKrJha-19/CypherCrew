from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

from app.extensions import db

from app.models import (
    User,
    Permission,
    UserPermission
)


permissions_bp = Blueprint(
    "permissions",
    __name__,
    url_prefix="/permissions"
)


def super_admin_required():
    return current_user.role == "super_admin"


@permissions_bp.route("/")
@login_required
def list_permissions():

    if not super_admin_required():
        return redirect(url_for("dashboard.index"))

    users = User.query.order_by(
        User.name.asc()
    ).all()

    return render_template(
        "permissions/list.html",
        users=users
    )


@permissions_bp.route("/user/<int:user_id>", methods=["GET", "POST"])
@login_required
def user_permissions(user_id):

    if not super_admin_required():
        return redirect(url_for("dashboard.index"))

    user = User.query.get_or_404(user_id)

    permissions = Permission.query.order_by(
        Permission.name.asc()
    ).all()

    if request.method == "POST":

        UserPermission.query.filter_by(
            user_id=user.id
        ).delete()

        selected_permissions = request.form.getlist(
            "permissions"
        )

        for permission_id in selected_permissions:

            db.session.add(
                UserPermission(
                    user_id=user.id,
                    permission_id=int(permission_id)
                )
            )

        db.session.commit()

        flash(
            "Permissions updated successfully.",
            "success"
        )

        return redirect(
             url_for("permissions.list_permissions")
        )
        

    assigned = {
        item.permission_id
        for item in user.permissions
    }

    return render_template(
        "permissions/user_permissions.html",
        user=user,
        permissions=permissions,
        assigned=assigned
    )