"""Everything one client's dashboard needs, in grouped queries.

The app had no per-client aggregate at all. The only delivery number anywhere
was ClientDeliverable.completed_count on the client page - a denormalised
counter maintained in four separate places AND hand-editable, so it can drift
from what actually happened and nothing said so. The one aggregate that was
written, build_top_clients() in routes/dashboard.py, issues a COUNT per client
in a Python loop and no route calls it.

So this counts Task rows, and reports the stored counter beside the real one
rather than in place of it.

Two rules run through the whole module:

  * `created_at` is stored UTC; `completed_at` and `employee_completed_at` are
    stored IST (they are stamped with ist_now()). Filtering the first needs
    ist_date(), the second must NOT use it. Getting this wrong drops or
    double-counts everything between IST 00:00 and 05:30 - the single most
    likely bug in this file.

  * Void never counts. task_status.EXCLUDED_FROM_METRICS is the codebase-wide
    rule and a voided task is not work that happened.
"""

from datetime import datetime, timedelta

from sqlalchemy import case, func

from app.extensions import db
from app.models import (
    ClientDeliverable, ClientMonthlyTarget, Task, User,
)
from app.utils import task_status
from app.utils.timezone import IST_OFFSET, ist_date

#: How many rows the "who delivered" and "recent deliveries" panels show. Long
#: enough to be a list, short enough that nobody scrolls a dashboard.
TOP_N = 6
RECENT_N = 8


def _live(query):
    """Anything that is not Void."""
    return query.filter(Task.status.notin_(task_status.EXCLUDED_FROM_METRICS))


def _delivered(client_id, start, end):
    """Tasks this client actually took delivery of in the window.

    Published AND stamped: completed_at is nulled again when a manager pulls a
    task back out of Published (routes/tasks.py:180-185), so the status alone
    would keep counting work that has been withdrawn.
    """
    q = Task.query.filter(
        Task.client_id == client_id,
        Task.status == task_status.PUBLISHED,
        Task.completed_at.isnot(None),
    )
    if start and end:
        # Already IST - a plain date() is exact here. See the module docstring.
        q = q.filter(db.func.date(Task.completed_at) >= start,
                     db.func.date(Task.completed_at) <= end)
    return q


def _counts_by_status(client_id):
    """{status: n} for this client's non-Void tasks, in one query."""
    rows = (
        db.session.query(Task.status, func.count(Task.id))
        .filter(Task.client_id == client_id,
                Task.status.notin_(task_status.EXCLUDED_FROM_METRICS))
        .group_by(Task.status)
        .all()
    )
    return {status: n for status, n in rows}


def _delta(current, previous):
    """(direction, percent) against the previous window.

    None percent when there is nothing to compare against - "up 100%" from
    zero is a sentence about arithmetic, not about the work.
    """
    if previous == 0:
        return ("up" if current else "neutral"), None
    change = round((current - previous) / previous * 100)
    return ("up" if change > 0 else "down" if change < 0 else "neutral"), change


def _month_target(client_id, on_date):
    """The ClientMonthlyTarget row covering `on_date`, or None.

    Targets are stored per calendar month, so a window that is not a month
    (7 days, a custom range) still reports against the month it ends in -
    which is what "are we on track" means to the person asking.
    """
    return (
        ClientMonthlyTarget.query
        .filter_by(client_id=client_id, month=on_date.month, year=on_date.year)
        .first()
    )


def _service_lines(client_id, target):
    """Per-service target vs delivered, with the stored counter beside the
    real one.

    completed_count is maintained in four places and can also be typed in by
    hand on the client page. Where it disagrees with the tasks actually
    published against that deliverable, the dashboard says so rather than
    quietly picking one of the two numbers.
    """
    if target is None:
        return []

    deliverables = (
        ClientDeliverable.query
        .filter_by(monthly_target_id=target.id)
        .order_by(ClientDeliverable.service_name,
                  ClientDeliverable.deliverable_name)
        .all()
    )
    if not deliverables:
        return []

    # One grouped count for every deliverable, rather than one query each.
    actual = dict(
        db.session.query(Task.deliverable_id, func.count(Task.id))
        .filter(Task.deliverable_id.in_([d.id for d in deliverables]),
                Task.status == task_status.PUBLISHED,
                Task.completed_at.isnot(None))
        .group_by(Task.deliverable_id)
        .all()
    )

    lines = []
    for d in deliverables:
        real = actual.get(d.id, 0)
        stored = d.completed_count or 0
        lines.append({
            "deliverable": d,
            "service": d.service_name,
            "name": d.deliverable_name,
            "target": d.target_count or 0,
            "delivered": real,
            "stored": stored,
            "drift": real - stored,
            "percent": (round(real / d.target_count * 100)
                        if d.target_count else None),
        })
    return lines


def _trend(client_id, start, end):
    """Published-per-day across the window, zero-filled.

    The series is "counts", not "values": Jinja resolves
    {{ trend.values }} to dict.values - the bound method - and
    renders it into the page instead of the numbers.

    Zero-filled on purpose: a line chart that skips empty days draws a slope
    between two deliveries a week apart and implies steady output.
    """
    if not start or not end:
        return {"labels": [], "counts": []}

    rows = dict(
        db.session.query(db.func.date(Task.completed_at), func.count(Task.id))
        .filter(Task.client_id == client_id,
                Task.status == task_status.PUBLISHED,
                Task.completed_at.isnot(None),
                db.func.date(Task.completed_at) >= start,
                db.func.date(Task.completed_at) <= end)
        .group_by(db.func.date(Task.completed_at))
        .all()
    )

    # SQLite hands back a string here, Postgres a date. Normalise to date.
    normalised = {}
    for key, count in rows.items():
        if isinstance(key, str):
            key = datetime.strptime(key, "%Y-%m-%d").date()
        normalised[key] = count

    labels, counts = [], []
    day = start
    while day <= end:
        labels.append(day.strftime("%d %b"))
        counts.append(normalised.get(day, 0))
        day += timedelta(days=1)

    return {"labels": labels, "counts": counts}


def _top_people(client_id, start, end):
    """Who actually delivered for this client in the window."""
    q = (
        db.session.query(User, func.count(Task.id).label("n"))
        .join(Task, Task.assigned_to_id == User.id)
        .filter(Task.client_id == client_id,
                Task.status == task_status.PUBLISHED,
                Task.completed_at.isnot(None))
    )
    if start and end:
        q = q.filter(db.func.date(Task.completed_at) >= start,
                     db.func.date(Task.completed_at) <= end)

    rows = (q.group_by(User.id)
             .order_by(func.count(Task.id).desc())
             .limit(TOP_N).all())
    return [{"user": user, "delivered": n} for user, n in rows]


def _turnaround(client_id, start, end):
    """Average created -> published, and how long the client sat on approvals.

    created_at is UTC and completed_at is IST, so the raw difference is 5h30m
    too large; IST_OFFSET is subtracted back out. client_review_seconds is
    already accumulated by record_status_time and nothing has ever shown it -
    it is the one number that says how much of the wait was the client's.
    """
    rows = (
        db.session.query(Task.created_at, Task.completed_at,
                         Task.client_review_seconds)
        .filter(Task.client_id == client_id,
                Task.status == task_status.PUBLISHED,
                Task.completed_at.isnot(None))
    )
    if start and end:
        rows = rows.filter(db.func.date(Task.completed_at) >= start,
                           db.func.date(Task.completed_at) <= end)

    spans, review = [], []
    for created, completed, review_seconds in rows.all():
        if created and completed:
            seconds = (completed - created).total_seconds() - \
                IST_OFFSET.total_seconds()
            if seconds >= 0:
                spans.append(seconds)
        if review_seconds:
            review.append(review_seconds)

    return {
        "turnaround_seconds": (sum(spans) / len(spans)) if spans else None,
        "review_seconds": (sum(review) / len(review)) if review else None,
        "measured": len(spans),
    }


def build_dashboard(client, period):
    """Every panel, in one call. `period` is a periods.resolve_period() dict."""
    cid = client.id
    start, end = period.get("start"), period.get("end")
    prev_start, prev_end = period.get("prev_start"), period.get("prev_end")

    delivered = _delivered(cid, start, end).count()
    previous = (_delivered(cid, prev_start, prev_end).count()
                if prev_start and prev_end else 0)
    direction, percent = _delta(delivered, previous)

    by_status = _counts_by_status(cid)
    in_review = (by_status.get(task_status.CORE_REVIEW, 0)
                 + by_status.get(task_status.CLIENT_REVIEW, 0))

    today = (period.get("today") and datetime.strptime(
        period["today"], "%Y-%m-%d").date())

    overdue = _live(Task.query.filter(
        Task.client_id == cid,
        Task.status.in_(task_status.OVERDUE_STATUSES),
        Task.deadline.isnot(None),
        db.func.date(Task.deadline) < today,
    )).count() if today else 0

    due_today = _live(Task.query.filter(
        Task.client_id == cid,
        Task.status.notin_(task_status.TERMINAL_STATUSES),
        Task.deadline.isnot(None),
        db.func.date(Task.deadline) == today,
    )).count() if today else 0

    target = _month_target(cid, end or datetime.utcnow().date())
    lines = _service_lines(cid, target)
    target_total = sum(line["target"] for line in lines)
    delivered_against_target = sum(line["delivered"] for line in lines)

    return {
        "client": client,
        "period": period,
        "delivered": delivered,
        "delivered_previous": previous,
        "delivered_direction": direction,
        "delivered_percent": percent,
        "in_progress": by_status.get(task_status.IN_PROGRESS, 0),
        "in_review": in_review,
        "overdue": overdue,
        "due_today": due_today,
        "total_live": sum(by_status.values()),
        "by_status": by_status,
        # Target lives per calendar month; a non-month window still reports
        # against the month it ends in.
        "target": target,
        "service_lines": lines,
        "target_total": target_total,
        "delivered_against_target": delivered_against_target,
        "target_percent": (round(delivered_against_target / target_total * 100)
                           if target_total else None),
        "has_drift": any(line["drift"] for line in lines),
        "trend": _trend(cid, start, end),
        "top_people": _top_people(cid, start, end),
        "recent": _delivered(cid, start, end)
                  .order_by(Task.completed_at.desc()).limit(RECENT_N).all(),
        **_turnaround(cid, start, end),
    }
