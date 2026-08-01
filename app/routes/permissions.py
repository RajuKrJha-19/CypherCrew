"""Who may do what, per person.

The screen behind the role catalog. A role hands somebody a sensible
starting set; this is where the exceptions get made - the junior trusted
with approvals for a month, the developer who also runs the publishing
queue - and where you can see, at a glance, how far someone has drifted
from their role's defaults.

Three guards matter here and none of them existed before, because the
whole blueprint was owner-only and the `manage_permissions` grant did
nothing at all. Now that it works, delegating it must not be the same as
handing over the company:

  * you cannot edit your own permissions (no self-escalation, and no
    locking yourself out either),
  * you cannot edit an administrator's,
  * and you cannot give away `manage_permissions` or `manage_users` -
    only the owner can create another person who hands out access.
"""

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from app.extensions import db

from app.models import Permission, User

from app.utils import roles
from app.utils.permissions import (
    ALL_CODES,
    CATEGORIES,
    DEPRECATED_CODES,
    can_manage_permissions,
    description as permission_description,
    granted_codes,
    set_permissions,
)


permissions_bp = Blueprint(
    "permissions",
    __name__,
    url_prefix="/permissions"
)


#: Permissions that hand out permissions. Only the owner may grant or
#: revoke these - otherwise one delegated grant is a two-click takeover.
META_CODES = frozenset({"manage_permissions", "manage_users"})


def _may_edit(target):
    """May the signed-in user change this person's permissions?"""

    if not can_manage_permissions(current_user):
        return False

    if roles.is_owner(current_user.role):
        return True

    if target.id == current_user.id:
        return False

    return not roles.is_management(target.role)


def _refuse(message):
    flash(message, "error")
    return redirect(url_for("permissions.list_permissions"))


@permissions_bp.route("/")
@login_required
def list_permissions():

    if not can_manage_permissions(current_user):
        return redirect(url_for("dashboard.index"))

    users = User.query.order_by(User.name.asc()).all()

    # How far each person sits from their role's defaults. A count of
    # permissions says nothing useful once there are eighteen of them;
    # "Senior Video Editor, +1 / -1" is the thing worth reading.
    summary = {}

    for user in users:
        if roles.is_owner(user.role):
            summary[user.id] = {"owner": True}
            continue

        held = granted_codes(user)
        defaults = roles.defaults_for(user.role)

        summary[user.id] = {
            "owner": False,
            "count": len(held),
            "added": sorted(held - defaults),
            "removed": sorted(defaults - held),
        }

    return render_template(
        "permissions/list.html",
        users=users,
        summary=summary,
    )


@permissions_bp.route("/user/<int:user_id>", methods=["GET", "POST"])
@login_required
def user_permissions(user_id):

    if not can_manage_permissions(current_user):
        return redirect(url_for("dashboard.index"))

    user = User.query.get_or_404(user_id)

    if not _may_edit(user):
        return _refuse(
            "You cannot change permissions for your own account or for "
            "an administrator."
        )

    if request.method == "POST":

        # Submitted by CODE, not by row id. Ids are guessable and the old
        # form validated nothing beyond "is it a number", so a hand-made
        # post could attach any permission row that happened to exist.
        submitted = {
            code for code in request.form.getlist("permissions")
            if code in ALL_CODES
        }

        held = granted_codes(user)

        refused = set()

        if not roles.is_owner(current_user.role):
            # Leave the meta permissions exactly as they were rather than
            # rejecting the whole save - the rest of the form is a
            # legitimate edit and should still land. But say so: dropping a
            # tick without a word, under a flash that reads "Permissions
            # updated", is indistinguishable from a save that did not work.
            for code in META_CODES:
                if code in held:
                    if code not in submitted:
                        refused.add(code)
                    submitted.add(code)
                else:
                    if code in submitted:
                        refused.add(code)
                    submitted.discard(code)

        # Retired codes are not on the form, so a save would silently drop
        # one that is still held. Keep it until somebody removes it on
        # purpose.
        submitted |= (held & DEPRECATED_CODES)

        added, removed = set_permissions(
            user, submitted, granted_by=current_user, commit=True,
        )

        if added or removed:
            flash(
                f"Permissions updated for {user.name} "
                f"(+{len(added)} / -{len(removed)}).",
                "success",
            )
        elif not refused:
            flash("No permission changes to save.", "info")

        if refused:
            display = {
                p.code: p.name for p in Permission.query
                .filter(Permission.code.in_(refused)).all()
            }
            names = ", ".join(
                display.get(code, code) for code in sorted(refused)
            )
            flash(
                f"{names} was left unchanged — only the owner can grant or "
                f"revoke a permission that hands out permissions.",
                "error",
            )

        return redirect(url_for("permissions.list_permissions"))

    held = granted_codes(user)
    defaults = roles.defaults_for(user.role)

    by_code = {p.code: p for p in Permission.query.all()}

    # Grouped by the part of the product they touch. Eighteen checkboxes in
    # one column is a list nobody reads; six short groups is a set of
    # decisions. Retired codes appear only if this person still holds one.
    groups = []

    for label, codes in CATEGORIES:
        rows = [
            {
                "code": code,
                "name": by_code[code].name if code in by_code else code,
                "description": permission_description(code),
                "held": code in held,
                "is_default": code in defaults,
                "locked": code in META_CODES
                and not roles.is_owner(current_user.role),
            }
            for code in codes
        ]
        groups.append((label, rows))

    retired = [
        {
            "code": code,
            "name": by_code[code].name if code in by_code else code,
            "description": permission_description(code),
            "held": True,
            "is_default": False,
            "locked": False,
        }
        for code in sorted(held & DEPRECATED_CODES)
    ]

    if retired:
        groups.append(("Retired", retired))

    return render_template(
        "permissions/user_permissions.html",
        user=user,
        groups=groups,
        default_codes=defaults,
        added=sorted(held - defaults),
        removed=sorted(defaults - held),
        is_owner_account=roles.is_owner(user.role),
    )


@permissions_bp.route("/user/<int:user_id>/apply-role-defaults",
                      methods=["POST"])
@login_required
def apply_defaults(user_id):
    """Reset somebody to exactly what their role starts with.

    POST-only and its own form, deliberately: it replaces the whole set,
    so it must never be reachable by a stray Enter keypress inside the
    checkbox form it sits next to.
    """

    if not can_manage_permissions(current_user):
        return redirect(url_for("dashboard.index"))

    user = User.query.get_or_404(user_id)

    if not _may_edit(user):
        return _refuse(
            "You cannot change permissions for your own account or for "
            "an administrator."
        )

    if roles.is_owner(user.role):
        return _refuse("The owner already has every permission.")

    codes = set(roles.defaults_for(user.role))

    if not roles.is_owner(current_user.role):
        # The same META_CODES fence user_permissions applies to the checkbox
        # form. Without it this button was the way around it: user_permissions
        # carefully refuses to let a non-owner grant manage_users or
        # manage_permissions, and then "Apply role defaults" wrote the role's
        # entire default set with no filter at all.
        #
        # No role's defaults contain a meta code today, so this is not
        # exploitable right now - it is one catalog edit away from being a
        # two-click escalation, and the edit would look completely innocuous.
        # Fencing both paths identically means the catalog cannot become a
        # privilege decision by accident.
        held = granted_codes(user)
        for code in META_CODES:
            if code in held:
                codes.add(code)
            else:
                codes.discard(code)

    set_permissions(user, codes, granted_by=current_user)
    db.session.commit()

    flash(
        f"Applied the {roles.label(user.role)} defaults to {user.name} "
        f"({len(codes)} permissions).",
        "success",
    )

    return redirect(url_for("permissions.user_permissions", user_id=user.id))
