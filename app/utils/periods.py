"""Turning a ?period= query string into a concrete date window.

Shared by the dashboard's Performance band and the user performance page.
Kept here rather than in either route module because two copies of date
maths drift: the moment one of them fixes an off-by-one or adds a preset,
the other quietly disagrees about what "last 7 days" means.

A window is inclusive at both ends and expressed in IST dates, because
that is the day boundary the team actually works to.
"""

from datetime import datetime, timedelta

from app.utils.timezone import ist_now

#: Custom ranges are capped so a per-day loop over the window stays bounded
#: however wide a range someone types into the two date inputs.
MAX_PERIOD_DAYS = 92

#: The presets, in the order they are offered. "all" is opt-in per caller -
#: see resolve_period(allow_all=...).
PRESETS = ("all", "today", "yesterday", "7d", "30d", "custom")


def parse_date(value):
    """A YYYY-MM-DD form value, or None if it is missing or malformed."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def format_range_label(start, end):
    if start == end:
        return start.strftime("%d %b %Y")
    if start.year == end.year:
        return f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
    return f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"


def resolve_period(args, allow_all=False, default="7d"):
    """Resolve the request's query string into a date window.

    Returns a dict with the window, a human label for the header, and the
    equally-sized window immediately before it so a caller can draw
    vs-previous deltas.

    `allow_all` opts a page into the unbounded "All time" preset, which
    returns start/end of None. It is off by default: the dashboard's band
    counts per day across the window and has no meaningful answer for an
    unbounded one, so it must not be reachable there by hand-typing
    ?period=all.
    """

    today = ist_now().date()
    key = (args.get("period") or default).lower()

    # Normalise first: an unknown key - or "all" on a page that doesn't
    # offer it - becomes this page's own default, rather than silently
    # landing on a window the caller never chose.
    if key not in PRESETS or (key == "all" and not allow_all):
        key = default if default in PRESETS else "7d"
        if key == "all" and not allow_all:
            key = "7d"

    if key == "all":
        # No window at all. Deltas are meaningless against "everything",
        # so prev_* stay None and callers skip the comparison.
        return {
            "key": "all",
            "label": "All time",
            "start": None,
            "end": None,
            "from": "",
            "to": "",
            "prev_start": None,
            "prev_end": None,
            "span_days": None,
            "today": today.isoformat(),
            "is_all_time": True,
        }

    if key == "today":
        start = end = today
        label = "Today"

    elif key == "yesterday":
        start = end = today - timedelta(days=1)
        label = "Yesterday"

    elif key == "30d":
        start, end = today - timedelta(days=29), today
        label = "Last 30 days"

    elif key == "custom":
        start = parse_date(args.get("from")) or today - timedelta(days=6)
        end = parse_date(args.get("to")) or today

        # A backwards range is a slip, not an intent - read it the way
        # the user clearly meant it rather than showing nothing.
        if start > end:
            start, end = end, start

        # Keep the per-day loop bounded regardless of what was typed.
        if (end - start).days > MAX_PERIOD_DAYS - 1:
            start = end - timedelta(days=MAX_PERIOD_DAYS - 1)

        label = format_range_label(start, end)
        key = "custom"

    else:
        # "7d" - the only preset left once the key has been normalised.
        key = "7d"
        start, end = today - timedelta(days=6), today
        label = "Last 7 days"

    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)

    return {
        "key": key,
        "label": label,
        "start": start,
        "end": end,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "prev_start": prev_start,
        "prev_end": prev_end,
        "span_days": span,
        "today": today.isoformat(),
        "is_all_time": False,
    }
