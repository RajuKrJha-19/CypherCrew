#: Plain-English meaning of each permission, keyed by Permission.code.
#:
#: Granting access is a decision with consequences, and the stored names
#: ("Approve Tasks", "Publish Tasks") do not say what they actually
#: unlock or how they differ. These are shown next to the checkboxes on
#: the permissions screen so that choice is made with the effect in
#: view. Kept in code rather than a new DB column so the wording can be
#: corrected without a migration.
DESCRIPTIONS = {
    "manage_users":
        "Create employee and admin accounts, edit their details and "
        "deactivate them.",

    "manage_permissions":
        "Grant and revoke these permissions for other users. Give this "
        "out sparingly - it lets someone extend their own team's "
        "access.",

    "manage_clients":
        "Add and edit clients, their services and their deliverables.",

    "view_client_stats":
        "See per-client performance figures and delivery history.",

    "edit_monthly_targets":
        "Set the monthly delivery targets that performance is measured "
        "against.",

    "manage_tasks":
        "Create, edit, assign and reassign any task, and move tasks "
        "between statuses on anyone's behalf. This is the main "
        "team-lead permission.",

    "approve_tasks":
        "Act on tasks in review: approve one to move it forward, or "
        "reject it back to the assignee with changes.",

    "publish_tasks":
        "Mark a task Published - the final sign-off that it is "
        "delivered and complete.",

    "view_reports":
        "Open the reports section and read submitted daily reports.",

    "manage_reports":
        "Submit and edit daily reports on behalf of the team.",
}


def description(code):
    """Plain-English meaning of a permission code, or "" if unknown."""
    return DESCRIPTIONS.get(code, "")


def has_permission(user, permission_code):

    if user.role == "super_admin":
        return True

    for item in user.permissions:

        if item.permission.code == permission_code:
            return True

    return False
