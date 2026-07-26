"""Single source of truth for task statuses.

Status names used to be hard-coded as string literals in eight
separate lists across the routes, which is how "Hold" ended up
half-wired: it was added to the calendar filter and two charts
but never to the board, the edit form or the time-tracking
accumulator, so tasks sitting in it silently lost their elapsed
time. Everything now derives from this module instead.
"""

# ---------------------------------------------------------------
# Status names
# ---------------------------------------------------------------

ASSIGNED = "Assigned"
IN_PROGRESS = "In Progress"
PAUSED = "Paused"
ON_HOLD = "On Hold"
CORE_REVIEW = "Core Review"
CLIENT_REVIEW = "Client Review"
SCHEDULED = "Scheduled"
PUBLISHED = "Published"
VOID = "Void"


# ---------------------------------------------------------------
# Plain-English meanings
# ---------------------------------------------------------------

#: What each status actually means, for tooltips on the board columns,
#: the status stepper and the time-tracking breakdown.
#:
#: The distinctions people get wrong are Paused vs On Hold (whose fault
#: the delay is, and who may clear it) and Void vs Published (both stop
#: the work, only one counts as delivered), so those say so explicitly.
#: Kept here next to the rules they describe - a description that lives
#: apart from the transition tables is one that quietly goes stale.
DESCRIPTIONS = {
    ASSIGNED:
        "Handed to the assignee but not started yet. "
        "Counts as pending work.",

    IN_PROGRESS:
        "Being worked on right now. The time tracker runs in "
        "this status.",

    PAUSED:
        "The assignee stopped work for now and can resume it "
        "themselves. The timer is stopped.",

    ON_HOLD:
        "Blocked by something outside the team, so the delay is "
        "not counted against the deadline. Only a manager can "
        "move it out. Needs a reason.",

    CORE_REVIEW:
        "Submitted for internal review. The team checks it before "
        "the client sees anything.",

    CLIENT_REVIEW:
        "With the client for approval.",

    SCHEDULED:
        "Approved and scheduled in Social Studio. It publishes "
        "automatically at the set time, then moves to Published.",

    PUBLISHED:
        "Approved and delivered. The task is complete.",

    VOID:
        "Cancelled, so the work will not resume. Left out of every "
        "performance metric - a cancelled job does not count "
        "against the team. Needs a reason.",
}


def description(status):
    """Plain-English meaning of `status`, or "" if unknown."""
    return DESCRIPTIONS.get(status, "")


# ---------------------------------------------------------------
# Groupings
# ---------------------------------------------------------------

#: Columns of the kanban board, in order.
BOARD_STATUSES = [
    ASSIGNED,
    IN_PROGRESS,
    PAUSED,
    ON_HOLD,
    CORE_REVIEW,
    CLIENT_REVIEW,
    SCHEDULED,
    PUBLISHED,
]

#: Nothing moves out of these on its own.
TERMINAL_STATUSES = [PUBLISHED, VOID]

#: Everything a task can legally be set to.
ALL_STATUSES = BOARD_STATUSES + [VOID]

#: Work is live: counts towards workload and "pending".
ACTIVE_STATUSES = [
    ASSIGNED,
    IN_PROGRESS,
    PAUSED,
    ON_HOLD,
    CORE_REVIEW,
    CLIENT_REVIEW,
]

#: Work has stopped and will not resume. A voided task was cancelled
#: by the client, so counting it would penalise the team for something
#: outside their control - it is excluded from every metric, including
#: the completion rate (it is neither completed nor pending).
EXCLUDED_FROM_METRICS = [VOID]

#: Statuses where no one is actively working, so the timer is paused.
TIMER_STOPPED_STATUSES = [PAUSED, ON_HOLD, VOID]

#: Statuses where a passed deadline is genuinely the team's problem.
#: On Hold is excluded because the delay belongs to whoever is
#: blocking it, and Void because the work was cancelled outright.
OVERDUE_STATUSES = [ASSIGNED, IN_PROGRESS, PAUSED]

#: These cannot be set without a written reason, so they are never
#: offered in a plain dropdown or reachable by dragging a card.
REASON_REQUIRED_STATUSES = [ON_HOLD, VOID]

#: Statuses a plain <select> may offer, i.e. everything that does not
#: need a reason captured alongside it.
SELECTABLE_STATUSES = [
    status for status in BOARD_STATUSES
    if status not in REASON_REQUIRED_STATUSES
]


# ---------------------------------------------------------------
# Filter groups
# ---------------------------------------------------------------

#: Awaiting a sign-off decision from someone other than the assignee.
#: Two distinct statuses, one thing to a manager: "finished, not yet
#: approved".
REVIEW_STATUSES = [CORE_REVIEW, CLIENT_REVIEW]

NEEDS_REVIEW = "Needs Review"

#: Named sets the task-list status filter accepts in place of a single
#: status. They exist because the dashboard counts tasks by *group*
#: ("Needs Review" = Core + Client Review) while the filter only ever
#: matched one exact status - so that KPI card linked to Core Review
#: alone and silently dropped every Client Review task it had just
#: counted. A group is a first-class filter value rather than a
#: hard-coded pair in the query, so the count, the link and the
#: dropdown option all read from this one definition.
#:
#: Keys must not collide with a real status name - they share the
#: ?status= parameter, and a real status always wins the lookup.
STATUS_GROUPS = {
    NEEDS_REVIEW: REVIEW_STATUSES,
}


def group_members(value):
    """The statuses a filter group expands to, or None when `value` is
    not a group (a real status, or empty)."""

    if value in ALL_STATUSES:
        return None

    return STATUS_GROUPS.get(value)


# ---------------------------------------------------------------
# Time tracking
# ---------------------------------------------------------------

#: status -> Task column accumulating seconds spent in that status.
DURATION_FIELD = {
    ASSIGNED: "pending_seconds",
    IN_PROGRESS: "in_progress_seconds",
    PAUSED: "paused_seconds",
    ON_HOLD: "on_hold_seconds",
    CORE_REVIEW: "core_review_seconds",
    CLIENT_REVIEW: "client_review_seconds",
    SCHEDULED: "scheduled_seconds",
    PUBLISHED: "published_seconds",
    VOID: "void_seconds",
}


# ---------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------

#: What an employee may do with a task assigned to them.
#: Paused is theirs to control; On Hold is not - a task is put on
#: hold because something outside the team blocks it, so only a
#: manager decides when that block has cleared.
EMPLOYEE_MOVES = {
    ASSIGNED: [IN_PROGRESS],
    # Assigned is allowed as an "undo": if a task was started (or paused)
    # by mistake, the assignee can reset it to Assigned rather than being
    # stuck moving it forward. The timer is stopped on the way (see the
    # status-change handler), so time is not counted against an unstarted
    # task.
    IN_PROGRESS: [ASSIGNED, PAUSED, CORE_REVIEW],
    PAUSED: [ASSIGNED, IN_PROGRESS],
    ON_HOLD: [],
    # lets an employee pull back a submission made by mistake
    CORE_REVIEW: [ASSIGNED, IN_PROGRESS, PAUSED],
    CLIENT_REVIEW: [],
    # Scheduled is driven by Social Studio (approved post scheduled to auto-
    # publish); an employee can't move it by hand.
    SCHEDULED: [],
    PUBLISHED: [],
    VOID: [],
}

#: What a user with manage_tasks may do: move a task from any status
#: to any other, in either direction - including undoing a Published
#: task or pulling one back out of Void. Nothing is terminal for a
#: manager. On Hold and Void still require a written reason (enforced
#: in the status-change route, and kept out of the clickable stepper
#: bubbles) so they never happen as a bare click even though they are
#: listed as destinations here - a manager can move a task *out* of
#: either into anything else on the board the same way.
MANAGER_MOVES = {
    status: [s for s in ALL_STATUSES if s != status]
    for status in ALL_STATUSES
}


def allowed_moves(status, can_manage):
    """Statuses `status` may move to, for this permission level."""
    table = MANAGER_MOVES if can_manage else EMPLOYEE_MOVES
    return table.get(status, [])


def can_move(status, new_status, can_manage):
    return new_status in allowed_moves(status, can_manage)


def duration_field(status):
    return DURATION_FIELD.get(status)


def css_modifier(status):
    """'On Hold' -> 'on-hold', for status-* CSS class names."""
    return status.lower().replace(" ", "-")
