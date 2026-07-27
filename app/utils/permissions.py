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

    "manage_social":
        "Use Social Studio - compose, schedule and publish social posts for "
        "clients, and manage drafts, the calendar and approvals.",

    "connect_social_accounts":
        "Connect and disconnect the social channels (Facebook Pages, linked "
        "Instagram accounts) that Social Studio publishes to.",
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


def can_manage_clients(user):
    """True for anyone allowed to *curate* a client record - editing the
    client's own details, its deliverables/targets, its sub-clients, and
    adding or deleting brand assets.

    Both admin roles qualify outright, on top of the explicit
    manage_clients permission. That widening is the point: the client
    page is readable by the whole team (everyone needs the logo, fonts
    and brand guidelines to do creative work), so "who may look" and
    "who may change things" are now genuinely different questions and
    the second one needs a name of its own rather than a
    has_permission() call repeated at a dozen call sites.

    Read access is deliberately NOT expressed here - it is simply
    @login_required. See clients.client_detail.
    """

    if user is None:
        return False

    return (
        user.role in ("admin", "super_admin")
        or has_permission(user, "manage_clients")
    )


def can_use_social(user):
    """True for anyone allowed to work in Social Studio - composing,
    scheduling and publishing posts, and the drafts/calendar/approvals
    around them.

    Both admin roles qualify outright, on top of the explicit
    manage_social permission. Admins already count as the social team
    everywhere else (they are notified on every task handoff, and they
    alone operate the engine's retry/requeue machinery), so leaving them
    locked out of the surface those notifications point at was a gap, not
    a policy. manage_social stays as the way to hand Studio to an
    employee who is not an admin.
    """

    if user is None:
        return False

    return (
        user.role in ("admin", "super_admin")
        or has_permission(user, "manage_social")
    )


def can_connect_social_accounts(user):
    """True for anyone allowed to connect or disconnect the channels
    Studio publishes to, and to bind a channel to a client.

    Widened to both admin roles for the same reason as can_use_social:
    Channels is part of the Social tab, and an admin who can compose but
    cannot connect a Page would find half the tab dead. The explicit
    connect_social_accounts permission still covers non-admins.
    """

    if user is None:
        return False

    return (
        user.role in ("admin", "super_admin")
        or has_permission(user, "connect_social_accounts")
    )


def can_manage_social_engine(user):
    """True for anyone allowed to operate the Social Publishing *engine* -
    the internal machinery: kicking the worker/scheduler, retrying or
    requeuing failed jobs, and other maintenance controls.

    This is deliberately narrower than `manage_social` (which lets an
    employee compose, schedule and publish for clients). Engine controls
    are ops surface, not publishing surface: a normal employee or manager
    must never see or reach them. Only the owner (super_admin) and admins
    qualify - mirroring can_manage_clients' role widening, so no new DB
    permission/migration is needed. Backend routes call this directly; the
    same helper is a Jinja global so the UI hides what the route forbids.
    """
    if user is None:
        return False
    return getattr(user, "role", None) in ("admin", "super_admin")
