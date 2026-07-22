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
    IN_PROGRESS: [PAUSED, CORE_REVIEW],
    PAUSED: [IN_PROGRESS],
    ON_HOLD: [],
    # lets an employee pull back a submission made by mistake
    CORE_REVIEW: [ASSIGNED, IN_PROGRESS, PAUSED],
    CLIENT_REVIEW: [],
    PUBLISHED: [],
    VOID: [],
}

#: What a user with manage_tasks may do.
MANAGER_MOVES = {
    ASSIGNED: [IN_PROGRESS, ON_HOLD, VOID],
    IN_PROGRESS: [ASSIGNED, PAUSED, ON_HOLD, CORE_REVIEW, VOID],
    PAUSED: [ASSIGNED, IN_PROGRESS, ON_HOLD, VOID],
    ON_HOLD: [ASSIGNED, IN_PROGRESS, VOID],
    CORE_REVIEW: [ASSIGNED, IN_PROGRESS, PAUSED, ON_HOLD, CLIENT_REVIEW, VOID],
    CLIENT_REVIEW: [CORE_REVIEW, ON_HOLD, PUBLISHED, VOID],
    PUBLISHED: [],
    # Terminal, but a manager can undo a mistaken void. It goes back
    # to Assigned rather than resuming mid-flight, because the work
    # has to be re-planned before anyone picks it up again.
    VOID: [ASSIGNED],
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
