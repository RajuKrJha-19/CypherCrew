"""What each permission unlocks, and the helpers that ask.

Permission *codes* are the vocabulary; app/utils/roles.py decides which
codes a role starts with. Keep the two apart: a role is a job title, a
permission is a capability, and the whole point of the rebuild is that one
does not silently imply the other.

Descriptions live here rather than in a database column so the wording can
be corrected without a migration - granting access is a decision with
consequences, and the stored names ("Approve Tasks", "Publish Tasks") do
not say what they actually unlock or how they differ.
"""

from app.utils import roles


#: Categories for the permissions screen, in display order. A flat list of
#: eighteen checkboxes is a list nobody reads; grouped by what part of the
#: product they touch, it is a set of decisions.
CATEGORIES = (
    ("Tasks", (
        "view_all_tasks",
        "assign_tasks",
        "manage_tasks",
        "approve_tasks",
        "publish_tasks",
    )),
    ("Social Studio", (
        "manage_social",
        "connect_social_accounts",
        "manage_social_engine",
    )),
    ("Clients", (
        "manage_clients",
        "edit_monthly_targets",
        "view_client_stats",
    )),
    ("Reports & insight", (
        "view_reports",
        "view_team_performance",
    )),
    ("People operations", (
        "manage_leaves",
        "manage_holidays",
        "manage_meetings",
    )),
    ("Administration", (
        "manage_users",
        "manage_permissions",
    )),
)


#: Codes that exist in the database but no longer mean anything. Hidden on
#: the permissions screen unless somebody still holds one, and no longer
#: seeded. The rows are deliberately NOT deleted - user_permissions has a
#: foreign key to them, and a dead grant is harmless where a broken FK is
#: not.
DEPRECATED_CODES = frozenset({
    # Described as "submit and edit daily reports on behalf of the team",
    # but no such route was ever written and nothing ever checked it.
    "manage_reports",
})


#: Plain-English meaning of each permission, keyed by Permission.code.
#: Shown next to the checkbox so the choice is made with the effect in view.
DESCRIPTIONS = {

    # -- Tasks ---------------------------------------------------------
    "view_all_tasks":
        "See every task in the company - the full board, list, calendar "
        "and gallery - instead of only the ones assigned to or shared "
        "with you. Read-only on its own: it does not allow editing.",

    "assign_tasks":
        "Create tasks for other people and reassign existing ones. "
        "Without it, someone can still create work for themselves.",

    "manage_tasks":
        "Edit any task, and override its status on anyone's behalf - put "
        "on hold, resume, void, restore, and bulk-edit. The senior "
        "team-lead permission.",

    "approve_tasks":
        "Act on work in Core Review: approve it through to Client Review, "
        "or send it back to the assignee with changes. This is the craft "
        "quality gate, not the client sign-off.",

    "publish_tasks":
        "The final client-facing sign-off: move a task from Client Review "
        "to Published, hand it to Social Studio, and approve or reject "
        "posts waiting in Studio's approval queue.",

    # -- Social Studio -------------------------------------------------
    "manage_social":
        "Use Social Studio - compose, schedule and publish social posts for "
        "clients, and manage drafts, the calendar and approvals.",

    "connect_social_accounts":
        "Connect and disconnect the social channels (Facebook Pages, linked "
        "Instagram accounts) that Social Studio publishes to, and decide "
        "which client each channel serves.",

    "manage_social_engine":
        "Operate the publishing machinery behind Studio: run the worker, "
        "requeue stuck or failed jobs, and process the queue by hand. Ops "
        "surface, not publishing surface.",

    # -- Clients -------------------------------------------------------
    "manage_clients":
        "Add and edit clients, their services and their brand assets.",

    "edit_monthly_targets":
        "Set the monthly delivery targets that performance is measured "
        "against.",

    "view_client_stats":
        "See per-client performance figures and delivery history, without "
        "being able to change anything about the client.",

    # -- Reports & insight ---------------------------------------------
    "view_reports":
        "Read the whole team's daily reports and timesheets, not just "
        "your own.",

    "view_team_performance":
        "Open the management dashboards: company-wide workload, delivery "
        "health, top performers, and any individual's performance page.",

    # -- People operations ---------------------------------------------
    "manage_leaves":
        "Record and remove leave for anyone on the team.",

    "manage_holidays":
        "Maintain the company holiday calendar everyone's deadlines are "
        "measured against.",

    "manage_meetings":
        "Schedule and cancel meetings, and invite participants.",

    # -- Administration ------------------------------------------------
    "manage_users":
        "Create accounts, edit people's details, reset their passwords and "
        "deactivate them. Limited to roles below your own.",

    "manage_permissions":
        "Grant and revoke these permissions for other users. Give this out "
        "sparingly - it is the permission that hands out permissions. The "
        "holder still cannot edit their own access, or an administrator's.",

    # -- Retired -------------------------------------------------------
    "manage_reports":
        "Retired - this never gated anything. Safe to remove.",
}


#: Every live code, in category order. The permissions screen renders from
#: this; the seeder creates from it; the role catalog is validated against
#: it. One list, so a new code cannot be half-added.
ALL_CODES = tuple(
    code for _label, codes in CATEGORIES for code in codes
)


def description(code):
    """Plain-English meaning of a permission code, or "" if unknown."""
    return DESCRIPTIONS.get(code, "")


def has_permission(user, permission_code):
    """Does `user` hold `permission_code`?

    The owner short-circuits to True and holds no rows at all, which is
    what makes the account impossible to lock out of its own system.
    Everyone else - admins included - is exactly what has been granted.
    """

    if user is None or not getattr(user, "is_authenticated", True):
        return False

    if roles.is_owner(getattr(user, "role", None)):
        return True

    for item in user.permissions:

        # A grant whose permission row was deleted underneath it: skip it
        # rather than raise. Nothing deletes permissions today, but this
        # function runs on every request and must never be the thing that
        # takes a page down.
        if item.permission is not None \
                and item.permission.code == permission_code:
            return True

    return False


def has_any(user, *permission_codes):
    """True if `user` holds at least one of these codes."""
    return any(has_permission(user, code) for code in permission_codes)


def granted_codes(user):
    """The permission codes this user actually holds, as a set.

    The owner's is empty even though they can do everything - the bypass
    is a rule, not a set of rows, and pretending otherwise on the
    permissions screen would invite someone to try revoking it.
    """
    if user is None:
        return set()

    return {
        item.permission.code
        for item in user.permissions
        if item.permission is not None
    }


def set_permissions(user, codes, granted_by=None, commit=False):
    """Make `user` hold exactly `codes`, and return (added, removed).

    A diff rather than the delete-everything-and-reinsert this replaces.
    That old shape rewrote every row on every save, so `granted_at` was
    always "now" for permissions nobody had touched in months, and two
    quick submits could race into duplicate rows.
    """
    from datetime import datetime

    from app.extensions import db
    from app.models import Permission, UserPermission

    wanted = {c for c in codes if c in DESCRIPTIONS}
    current = granted_codes(user)

    to_add = wanted - current
    to_remove = current - wanted

    if to_remove:
        removable_ids = [
            row.id for row in Permission.query
            .filter(Permission.code.in_(to_remove)).all()
        ]
        if removable_ids:
            UserPermission.query.filter(
                UserPermission.user_id == user.id,
                UserPermission.permission_id.in_(removable_ids),
            ).delete(synchronize_session=False)

    for permission in Permission.query.filter(
            Permission.code.in_(to_add)).all():
        db.session.add(UserPermission(
            user_id=user.id,
            permission_id=permission.id,
            granted_at=datetime.utcnow(),
            granted_by_id=getattr(granted_by, "id", None),
        ))

    if commit:
        db.session.commit()

    return to_add, to_remove


def apply_role_defaults(user, granted_by=None, commit=False):
    """Reset `user` to exactly the default permissions of their role.

    Deliberately a replace, not a merge: "apply defaults" has to be able to
    take away an override as well as add one, or it is not a reset. Returns
    the codes the user ends up with.
    """
    defaults = roles.defaults_for(getattr(user, "role", None))
    set_permissions(user, defaults, granted_by=granted_by, commit=commit)
    return defaults


def _is_management(user):
    """Owner or Admin - the administrators of the system itself.

    Several helpers below still widen to management by role rather than by
    permission. That is deliberate and it is what keeps this rebuild
    access-neutral on day one: an existing admin holds no permission rows,
    so swapping these for a pure has_permission() check would take away
    everything they can do today. Roles hand out defaults from here on, and
    these widenings can be retired once the existing admins have been given
    their defaults on the permissions screen.
    """
    return roles.is_management(getattr(user, "role", None))


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

    return _is_management(user) or has_permission(user, "manage_clients")


def can_view_all_tasks(user):
    """True for anyone who sees every task rather than only their own and
    the ones shared with them.

    Read scope used to ride on `manage_tasks`, which also meant "edit any
    task, hold it, void it, bulk-change it". Those are different questions:
    a senior editor needs the whole board in view to plan around it and has
    no business voiding someone else's work. `manage_tasks` is still
    accepted here so that today's team leads keep the visibility they
    already have.
    """

    if user is None:
        return False

    return has_any(user, "view_all_tasks", "manage_tasks")


def can_assign_tasks(user):
    """True for anyone who may create work for someone else, or move an
    existing task to a different assignee.

    Split out of `manage_tasks` so a craft senior can hand a job to a
    junior without also being able to override the whole board. Anyone may
    still create a task for themselves - that is self_assign_task, and it
    needs no permission at all.
    """

    if user is None:
        return False

    return has_any(user, "assign_tasks", "manage_tasks")


def can_view_team_performance(user):
    """True for anyone allowed to see the management dashboards: company
    workload, delivery health, top performers, and an individual's
    performance page.

    Previously this had no name and no guard - `/super-admin` and `/admin`
    were reachable by any signed-in user who typed the URL, and only the
    post-login redirect kept people away from them.
    """

    if user is None:
        return False

    return _is_management(user) or has_permission(user, "view_team_performance")


def can_manage_users(user):
    """True for anyone allowed to create and edit accounts.

    `manage_users` had been a decorative grant: it revealed the Users item
    in the sidebar while the routes themselves tested the role, so granting
    it produced a link that bounced you back to the dashboard. It works now.
    Which roles you may hand out is a separate, narrower question - see
    roles.assignable_by.
    """

    if user is None:
        return False

    return _is_management(user) or has_permission(user, "manage_users")


def can_manage_permissions(user):
    """True for anyone allowed to open the permissions screen.

    The owner always qualifies (via the has_permission short-circuit). For
    everybody else this is an explicit grant, and it is still fenced: the
    holder cannot edit their own permissions, cannot edit an
    administrator's, and cannot hand out this permission or manage_users.
    Those guards live in the routes - see routes/permissions.py.
    """

    if user is None:
        return False

    return has_permission(user, "manage_permissions")


def can_manage_leaves(user):
    """True for anyone allowed to record or remove someone's leave."""

    if user is None:
        return False

    # TRANSITIONAL: `manage_tasks` reproduces today's rule, where leave
    # administration rode along with the task-management permission. Drop
    # the second code once manage_leaves has been granted to the people who
    # should have it.
    return _is_management(user) or has_any(user, "manage_leaves", "manage_tasks")


def can_manage_holidays(user):
    """True for anyone allowed to maintain the company holiday calendar."""

    if user is None:
        return False

    # TRANSITIONAL: see can_manage_leaves.
    return _is_management(user) or has_any(user, "manage_holidays", "manage_tasks")


def can_manage_meetings(user):
    """True for anyone allowed to schedule or cancel meetings."""

    if user is None:
        return False

    # TRANSITIONAL: see can_manage_leaves.
    return _is_management(user) or has_any(user, "manage_meetings", "manage_tasks")


def can_review(user):
    """True for anyone with work waiting in the Review Queue - either gate:
    the craft one at Core Review, or the client sign-off at Client Review."""

    if user is None:
        return False

    return _is_management(user) or has_any(user, "approve_tasks", "publish_tasks")


def can_publish(user):
    """True for the final, client-facing sign-off: Client Review ->
    Published, the handoff into Social Studio, and approving posts waiting
    in Studio's queue.

    Separated from `approve_tasks`, which is now the craft gate at Core
    Review. A senior editor reviews a junior's cut; a manager is the one
    who says it goes to the client. `publish_tasks` finally means what its
    description always claimed.
    """

    if user is None:
        return False

    return has_permission(user, "publish_tasks")


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

    return _is_management(user) or has_permission(user, "manage_social")


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

    return _is_management(user) \
        or has_permission(user, "connect_social_accounts")


def can_manage_social_engine(user):
    """True for anyone allowed to operate the Social Publishing *engine* -
    the internal machinery: kicking the worker/scheduler, retrying or
    requeuing failed jobs, and other maintenance controls.

    This is deliberately narrower than `manage_social` (which lets an
    employee compose, schedule and publish for clients). Engine controls
    are ops surface, not publishing surface: a normal employee or manager
    must never see or reach them. Both admin roles qualify, and the
    `manage_social_engine` permission now makes it delegable - previously
    this was the one capability with no code behind it, so it could not be
    handed to whoever actually babysits the queue. Backend routes call this
    directly; the same helper is a Jinja global so the UI hides what the
    route forbids.
    """
    if user is None:
        return False
    return _is_management(user) or has_permission(user, "manage_social_engine")
