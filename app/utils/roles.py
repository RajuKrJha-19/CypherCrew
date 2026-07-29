"""The role catalog: every job role, what it is called, and what it may do
by default.

One list, because the alternative was proven unworkable. The three original
roles ("super_admin", "admin", "employee") were written as bare strings at
roughly seventy call sites with no enum, no constant and no database
constraint, so adding a role meant finding all seventy - and missing one is
silent. A user with an unlisted role simply disappeared from the assignee
dropdown, or vanished from the team dashboard, or (worst of all) was logged
out on sign-in, because `dashboard.index`'s if-chain fell through to logout.

Everything downstream is derived from ROLE_LIST below: the dropdowns, the
badge colours, the "who counts as management" checks, the "every real user"
queries and the dashboard each role lands on. Adding another role is one
entry here plus, if it needs different powers, one `defaults` set. Nothing
outside this module names a role value - verified by grep, and worth
keeping true.

The catalog is a four-rung ladder per discipline (Intern, the craft grade,
the senior craft grade, Manager). Values are stored strings and never
change once shipped; the ladder that preceded this one used senior_* and
junior_* prefixes, and migration a7c4e2f81d36 rewrites those in place.

Two things deliberately NOT here:

  * Permission *meanings* - those live in app/utils/permissions.py, which
    owns the codes, their descriptions and their categories. This module
    only says which codes a role starts with.

  * Any per-discipline data scoping. `discipline` is descriptive - it groups
    the dropdown and reads well on a profile. It does not filter what anyone
    can see. Roles gate actions, not rows.
"""

from dataclasses import dataclass, field


#: Tiers, coarsest to finest. Used for badge colour, for "may this person
#: administer that person" and for which dashboard someone lands on.
#:
#: OWNER and MANAGEMENT administer the *system*. LEAD downwards is the
#: craft ladder - a Social Media Manager runs a pod, not the software, so
#: they are deliberately not MANAGEMENT and land on the individual
#: dashboard like everyone else who delivers work.
TIER_OWNER = "owner"
TIER_MANAGEMENT = "management"
TIER_LEAD = "lead"
TIER_SENIOR = "senior"
TIER_JUNIOR = "junior"
TIER_INTERN = "intern"
TIER_GENERAL = "general"

#: Highest first. `assignable_by` uses this ordering to enforce that you may
#: only create or edit people below your own tier.
TIER_ORDER = (TIER_OWNER, TIER_MANAGEMENT, TIER_LEAD, TIER_SENIOR,
              TIER_JUNIOR, TIER_INTERN, TIER_GENERAL)


@dataclass(frozen=True)
class Role:
    """One job role. Frozen because the catalog is read-only at runtime -
    a role's powers change by editing this file and redeploying, never by
    something mutating the registry in a request."""

    #: Stored in users.role. Never changes once shipped - renaming a value
    #: would mean a data migration for a cosmetic gain (see `label`).
    value: str

    #: What people actually read. "super_admin" displays as "Owner"; the
    #: stored value stays as it is.
    label: str

    tier: str

    #: Groups the dropdown and reads on a profile. None for the two
    #: cross-cutting roles and for the legacy generic one.
    discipline: str = None

    #: Permission codes a new user of this role starts with, and what
    #: "Apply role defaults" restores. Empty for juniors: a junior's rights
    #: over their own queue are implicit (assignee branches in tasks.py and
    #: task_status.can_move), not granted.
    defaults: frozenset = field(default_factory=frozenset)


#: Display grouping for the role dropdown, in order.
GROUP_LEADERSHIP = "Leadership"
GROUP_SOCIAL = "Social Media"
GROUP_CREATIVE = "Creative"
GROUP_CONTENT = "Content"
GROUP_ENGINEERING = "Engineering"
GROUP_GENERAL = "General"


#: Permission bundles, named so the table below reads as intent rather than
#: as a wall of strings.
#:
#: The ladder is four rungs per discipline - Intern, the craft grade, the
#: senior craft grade, and Manager - and each rung is a real step in what
#: someone may do, not just a nicer word on a profile:
#:
#:   Intern            works only their own queue.
#:   Craft grade       same, plus the Studio where the craft needs it.
#:   Senior craft      sees the whole board, hands out work, and gates
#:                     quality at Core Review.
#:   Manager           all of that, plus the people-and-planning side:
#:                     restructuring work, leave, meetings, team numbers.
#:
#: Titles follow the craft rather than a single word. Agencies say "Senior
#: Video Editor", not "Senior Video Executive" - "Executive" is a social
#: media and account-management term, and using it everywhere would read
#: as invented.

#: Nothing granted. A junior's rights over their own queue are implicit
#: (the assignee branches in tasks.py and task_status.can_move), not
#: granted - so an empty set here is the correct, deliberate value.
_INTERN = frozenset()

_CRAFT = frozenset()

_SENIOR_CRAFT = frozenset({
    "view_all_tasks",
    "assign_tasks",
    "approve_tasks",
    "view_client_stats",
    "view_reports",
})

_CRAFT_MANAGER = _SENIOR_CRAFT | frozenset({
    "manage_tasks",
    "view_team_performance",
    "manage_leaves",
    "manage_meetings",
})

_ADMIN_ALL = frozenset({
    "manage_users",
    "manage_permissions",
    "manage_clients",
    "edit_monthly_targets",
    "view_client_stats",
    "view_all_tasks",
    "assign_tasks",
    "manage_tasks",
    "approve_tasks",
    "publish_tasks",
    "manage_social",
    "connect_social_accounts",
    "manage_social_engine",
    "manage_leaves",
    "manage_holidays",
    "manage_meetings",
    "view_reports",
    "view_team_performance",
})


ROLE_LIST = (

    # -- Leadership ----------------------------------------------------
    Role(
        value="super_admin",
        label="Owner",
        tier=TIER_OWNER,
        # Deliberately empty: has_permission() short-circuits True for the
        # owner. Storing rows would let someone revoke them and lock the
        # owner out of their own system.
        defaults=frozenset(),
    ),
    Role(
        value="admin",
        label="Admin",
        tier=TIER_MANAGEMENT,
        defaults=_ADMIN_ALL,
    ),

    # -- Social media --------------------------------------------------
    # The one discipline that keeps "Executive": in agencies that word
    # belongs to social and account work, and it is what the job is called.
    Role(
        value="social_media_intern",
        label="Social Media Intern",
        tier=TIER_INTERN,
        discipline="social_media",
        defaults=_INTERN,
    ),
    Role(
        value="social_media_executive",
        label="Social Media Executive",
        tier=TIER_JUNIOR,
        discipline="social_media",
        # Composes, schedules, engages. Approval belongs to the manager,
        # which is the whole point of the executive/manager split.
        defaults=frozenset({"manage_social"}),
    ),
    Role(
        value="social_media_senior_executive",
        label="Senior Social Media Executive",
        tier=TIER_SENIOR,
        discipline="social_media",
        defaults=frozenset({
            "view_all_tasks", "manage_social", "view_client_stats",
        }),
    ),
    Role(
        value="social_media_manager",
        label="Social Media Manager",
        tier=TIER_LEAD,
        discipline="social_media",
        # Owns clients end to end: plans the work, hands it out, signs it
        # off to the client, and runs the channels it goes out on.
        defaults=_CRAFT_MANAGER | frozenset({
            "publish_tasks",
            "manage_social", "connect_social_accounts",
            "manage_clients", "edit_monthly_targets",
        }),
    ),

    # -- Creative ------------------------------------------------------
    Role(
        value="video_editor_intern",
        label="Video Editing Intern",
        tier=TIER_INTERN,
        discipline="video_editor",
        defaults=_INTERN,
    ),
    Role(
        value="video_editor",
        label="Video Editor",
        tier=TIER_JUNIOR,
        discipline="video_editor",
        defaults=_CRAFT,
    ),
    Role(
        value="video_editor_senior",
        label="Senior Video Editor",
        tier=TIER_SENIOR,
        discipline="video_editor",
        defaults=_SENIOR_CRAFT,
    ),
    Role(
        value="video_editor_manager",
        label="Video Editing Manager",
        tier=TIER_LEAD,
        discipline="video_editor",
        defaults=_CRAFT_MANAGER,
    ),

    Role(
        value="graphic_designer_intern",
        label="Graphic Design Intern",
        tier=TIER_INTERN,
        discipline="graphic_designer",
        defaults=_INTERN,
    ),
    Role(
        value="graphic_designer",
        label="Graphic Designer",
        tier=TIER_JUNIOR,
        discipline="graphic_designer",
        defaults=_CRAFT,
    ),
    Role(
        value="graphic_designer_senior",
        label="Senior Graphic Designer",
        tier=TIER_SENIOR,
        discipline="graphic_designer",
        defaults=_SENIOR_CRAFT,
    ),
    Role(
        value="graphic_designer_manager",
        label="Design Manager",
        tier=TIER_LEAD,
        discipline="graphic_designer",
        defaults=_CRAFT_MANAGER,
    ),

    # -- Content -------------------------------------------------------
    Role(
        value="content_writer_intern",
        label="Content Intern",
        tier=TIER_INTERN,
        discipline="content_writer",
        defaults=_INTERN,
    ),
    Role(
        value="content_writer",
        label="Content Writer",
        tier=TIER_JUNIOR,
        discipline="content_writer",
        defaults=_CRAFT,
    ),
    Role(
        value="content_writer_senior",
        label="Senior Content Writer",
        tier=TIER_SENIOR,
        discipline="content_writer",
        defaults=_SENIOR_CRAFT,
    ),
    Role(
        value="content_writer_manager",
        label="Content Manager",
        tier=TIER_LEAD,
        discipline="content_writer",
        defaults=_CRAFT_MANAGER,
    ),

    # -- Engineering ---------------------------------------------------
    Role(
        value="software_developer_intern",
        label="Software Development Intern",
        tier=TIER_INTERN,
        discipline="software_developer",
        defaults=_INTERN,
    ),
    Role(
        value="software_developer",
        label="Software Developer",
        tier=TIER_JUNIOR,
        discipline="software_developer",
        defaults=_CRAFT,
    ),
    Role(
        value="software_developer_senior",
        label="Senior Software Developer",
        tier=TIER_SENIOR,
        discipline="software_developer",
        defaults=_SENIOR_CRAFT,
    ),
    Role(
        value="engineering_manager",
        label="Engineering Manager",
        tier=TIER_LEAD,
        discipline="software_developer",
        defaults=_CRAFT_MANAGER,
    ),

    # -- General -------------------------------------------------------
    Role(
        value="employee",
        label="Team Member",
        tier=TIER_GENERAL,
        # Everyone who predates this catalog is an `employee`, and their
        # existing grants are untouched. Defaults are empty on purpose:
        # "Apply role defaults" on a Team Member must never hand someone
        # powers they were not already given by hand.
    ),
)


ROLES = {role.value: role for role in ROLE_LIST}

#: Every role value. Replaces the `role.in_(["super_admin","admin",
#: "employee"])` filters that meant "every real user" - the ones that made
#: a person disappear from assignee, visibility, leave, meeting and
#: transfer pickers the moment they held any other role.
ALL_ROLE_VALUES = tuple(role.value for role in ROLE_LIST)

#: Owner + Admin. What the old `role in ("admin", "super_admin")` tests
#: meant. Kept as a role check (not a permission) only where the question
#: really is "is this person an administrator of the system".
MANAGEMENT_ROLES = tuple(
    role.value for role in ROLE_LIST
    if role.tier in (TIER_OWNER, TIER_MANAGEMENT)
)

#: Everyone who delivers work. Replaces `role == "employee"` in the team
#: dashboard queries (workload, company health, live team, active-employee
#: count, top performers), which returned nothing at all for a new role.
TEAM_MEMBER_ROLES = tuple(
    role.value for role in ROLE_LIST
    if role.value not in MANAGEMENT_ROLES
)

#: Who may be set as a client's owning manager. Its own name rather than an
#: alias of MANAGEMENT_ROLES, because "may a Senior Social Media Manager own
#: a client outright" is a policy question, and it should be answered here
#: deliberately rather than by a search-and-replace somewhere else.
CLIENT_MANAGER_ROLES = MANAGEMENT_ROLES

#: Fallback for a value that is not in the catalog - a role from an older
#: deploy, or hand-edited in the database.
_UNKNOWN_TIER = TIER_GENERAL


def get(value):
    """The Role for `value`, or None. Never raises - callers routinely pass
    whatever is in the database."""
    return ROLES.get(value)


def label(value):
    """Human-readable name. Falls back to prettifying the raw value so an
    unknown legacy role still reads as words rather than as a slug."""
    role = ROLES.get(value)
    if role is not None:
        return role.label
    return (value or "").replace("_", " ").title() or "Unknown"


def tier(value):
    role = ROLES.get(value)
    return role.tier if role is not None else _UNKNOWN_TIER


def is_owner(value):
    return tier(value) == TIER_OWNER


def is_management(value):
    """Owner or Admin - the people who administer the system itself."""
    return value in MANAGEMENT_ROLES


def badge_class(value):
    """CSS modifier for the role badge.

    Keyed on tier, not on the role value: fifteen roles would otherwise mean
    fifteen colour rules, and a sixteenth role would render unstyled until
    someone remembered the stylesheet."""
    return f"role-tier-{tier(value)}"


def dashboard_endpoint(value):
    """Which dashboard this role lands on after sign-in.

    Anything unrecognised gets the individual dashboard. That default is the
    point: the version of this logic it replaces fell through to
    `auth.logout`, so a user whose role was not one of exactly three strings
    was signed out every time they signed in."""
    role_tier = tier(value)
    if role_tier == TIER_OWNER:
        return "dashboard.super_admin"
    if role_tier == TIER_MANAGEMENT:
        return "dashboard.admin"
    return "dashboard.employee"


#: Dashboard endpoints, for the sidebar's active-state test.
DASHBOARD_ENDPOINTS = (
    "dashboard.index",
    "dashboard.super_admin",
    "dashboard.admin",
    "dashboard.employee",
)


def defaults_for(value):
    """Permission codes a new user of this role starts with. Empty for an
    unknown role - never guess someone into extra access."""
    role = ROLES.get(value)
    return set(role.defaults) if role is not None else set()


#: Discipline -> dropdown group. A dict rather than an if-chain so adding a
#: discipline is one entry, and a discipline nobody mapped falls to General
#: instead of silently disappearing from the picker.
_DISCIPLINE_GROUP = {
    "social_media": GROUP_SOCIAL,
    "video_editor": GROUP_CREATIVE,
    "graphic_designer": GROUP_CREATIVE,
    "content_writer": GROUP_CONTENT,
    "software_developer": GROUP_ENGINEERING,
}


def _group_of(role):
    if role.tier in (TIER_OWNER, TIER_MANAGEMENT):
        return GROUP_LEADERSHIP
    return _DISCIPLINE_GROUP.get(role.discipline, GROUP_GENERAL)


GROUP_ORDER = (GROUP_LEADERSHIP, GROUP_SOCIAL, GROUP_CREATIVE, GROUP_CONTENT,
               GROUP_ENGINEERING, GROUP_GENERAL)


def grouped_options(values=None):
    """[(group label, [Role, ...])] for an <optgroup> dropdown, in catalog
    order, skipping empty groups. `values` restricts it - pass the result of
    assignable_by() so the form only offers what the server will accept."""
    allowed = None if values is None else set(values)
    grouped = []

    for group in GROUP_ORDER:
        members = [
            role for role in ROLE_LIST
            if _group_of(role) == group
            and (allowed is None or role.value in allowed)
        ]
        if members:
            grouped.append((group, members))

    return grouped


def assignable_by(user):
    """Role values `user` may give to somebody else.

    Strictly below their own tier, so an admin cannot mint another admin (or
    an owner), and cannot promote themselves sideways into one. The owner is
    the only account that can create management users, which keeps the
    top of the tree a deliberate act.

    Returns () for anyone who is not management - they have no business on
    the user-administration screens at all, and the route guard says so too.
    """
    if user is None:
        return ()

    actor_tier = tier(getattr(user, "role", None))

    if actor_tier == TIER_OWNER:
        # The owner may set any role, including another admin. Not
        # super_admin: a second owner should be a database-level decision,
        # not a dropdown.
        return tuple(v for v in ALL_ROLE_VALUES if not is_owner(v))

    if actor_tier == TIER_MANAGEMENT:
        return TEAM_MEMBER_ROLES

    return ()


def can_assign_role(user, value):
    """Server-side allowlist for the role field. The role <select> is built
    from assignable_by(), but a form post is just a string - this is what
    actually stops one."""
    return value in assignable_by(user)
