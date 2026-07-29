"""The posting queue - per-channel recurring slots + "next open slot".

A channel's posting schedule is its set of weekly `SocialPostingSlot`s.
"Add to queue" (composer) asks this service for the next slot that isn't
already filled, so posts land on a steady cadence without anyone hand-picking
a datetime. Everything is computed in IST (how people set times) and returned
as naive UTC (how the engine stores `scheduled_for`).
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.extensions import db
from app.models import SocialPostingSlot, SocialPostTarget
from app.utils.timezone import IST_OFFSET

# How far ahead we will look for a free slot before giving up (a channel with
# a full queue two months out is a problem to surface, not to loop on).
_HORIZON_DAYS = 62

# A sensible starting cadence for a channel that has set no slots of its own:
# weekday mornings/afternoons, IST. Minutes since midnight.
_DEFAULT_MINUTES = (10 * 60, 13 * 60, 17 * 60)      # 10:00, 13:00, 17:00
_DEFAULT_WEEKDAYS = (0, 1, 2, 3, 4)                 # Mon-Fri


def slots_for(account_id):
    """This channel's own slots, ordered by weekday then time."""
    return (SocialPostingSlot.query
            .filter_by(social_account_id=account_id)
            .order_by(SocialPostingSlot.weekday, SocialPostingSlot.minute)
            .all())


def default_slots():
    """Non-persisted stand-in slots for a channel that has set none, so
    'add to queue' always has somewhere to put a post."""
    return [SimpleNamespace(weekday=w, minute=m)
            for w in _DEFAULT_WEEKDAYS for m in _DEFAULT_MINUTES]


def effective_slots(account_id):
    """A channel's own slots, or the defaults if it has none."""
    return slots_for(account_id) or default_slots()


def next_open_slot(account_id, after=None):
    """The earliest future slot datetime (naive UTC) for this channel that no
    scheduled/queued post already occupies, or None if the queue is full for
    the horizon. `after` (UTC) defaults to now."""
    now = after or datetime.utcnow()
    slots = effective_slots(account_id)
    if not slots:
        return None

    by_day = {}
    for s in slots:
        by_day.setdefault(s.weekday, []).append(s.minute)
    for day in by_day:
        by_day[day] = sorted(set(by_day[day]))

    # Slot times already taken on this channel (exact match: we always assign
    # posts to the exact slot instant, so equality is enough).
    taken = {
        t.scheduled_for for t in SocialPostTarget.query.filter(
            SocialPostTarget.social_account_id == account_id,
            SocialPostTarget.status.in_(
                ["scheduled", "approved", "publishing"]),
            SocialPostTarget.scheduled_for.isnot(None),
        ).all()
    }

    now_ist = now + IST_OFFSET
    base = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(_HORIZON_DAYS):
        day = base + timedelta(days=offset)
        for minute in by_day.get(day.weekday(), ()):
            slot_ist = day + timedelta(minutes=minute)
            if slot_ist <= now_ist:
                continue                        # already past today
            slot_utc = slot_ist - IST_OFFSET
            if slot_utc in taken:
                continue                        # someone already has this slot
            return slot_utc
    return None


def set_slots(account_id, pairs, actor_id=None):
    """Replace a channel's slots with `pairs` of (weekday, minute). Additive
    model: the whole set is rewritten, which is how the settings screen edits
    it. Invalid pairs are dropped rather than raising."""
    clean = set()
    for wd, mn in pairs:
        try:
            wd, mn = int(wd), int(mn)
        except (TypeError, ValueError):
            continue
        if 0 <= wd <= 6 and 0 <= mn <= 1439:
            clean.add((wd, mn))

    SocialPostingSlot.query.filter_by(
        social_account_id=account_id).delete(synchronize_session=False)
    for wd, mn in sorted(clean):
        db.session.add(SocialPostingSlot(
            social_account_id=account_id, weekday=wd, minute=mn))
    db.session.commit()
    return len(clean)


def suggested_minutes(account_id, top=3):
    """Best-time hint: the hours this channel's own published posts got the
    most engagement, as minute-of-day values. Falls back to the sensible
    defaults when there isn't enough analytics history yet, so the feature is
    useful on day one and sharpens as data accrues."""
    from app.models import SocialAnalyticsSnapshot

    rows = (db.session.query(SocialPostTarget.scheduled_for,
                             SocialAnalyticsSnapshot.metrics)
            .join(SocialAnalyticsSnapshot,
                  SocialAnalyticsSnapshot.target_id == SocialPostTarget.id)
            .filter(SocialPostTarget.social_account_id == account_id,
                    SocialPostTarget.scheduled_for.isnot(None))
            .all())

    buckets = {}
    for when, metrics in rows:
        if not when or not isinstance(metrics, dict):
            continue
        eng = (metrics.get("engagement") or metrics.get("likes") or 0)
        try:
            eng = float(eng)
        except (TypeError, ValueError):
            eng = 0.0
        hour_ist = (when + IST_OFFSET).hour
        buckets[hour_ist] = buckets.get(hour_ist, 0.0) + eng

    if not buckets or all(v == 0 for v in buckets.values()):
        return list(_DEFAULT_MINUTES)
    best = sorted(buckets, key=lambda h: buckets[h], reverse=True)[:top]
    return [h * 60 for h in sorted(best)]
