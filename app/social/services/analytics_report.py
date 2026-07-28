"""Reading the analytics snapshots back out for the Analytics screen.

Snapshots are append-only: every sync writes a fresh row for a post, so a
post that has been synced ten times has ten rows with growing numbers.
Summing them all would multiply every figure by however many times the
cron happened to run, which is the obvious trap here. Only the LATEST
snapshot per post counts.

Posts are attributed to the period by when they were PUBLISHED, not when
the snapshot was taken - "reach in the last 7 days" means the reach of the
posts published in those 7 days, which is the question an agency actually
asks. A post published today whose numbers were synced an hour ago belongs
to today either way.
"""

from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    SocialAccount, SocialAnalyticsSnapshot, SocialPost, SocialPostTarget,
)

#: The figures the screen shows, in display order. Providers report a
#: superset (YouTube adds views, Meta adds saved); anything not listed is
#: still stored, just not tiled.
METRICS = [
    ("reach", "Reach", "fa-signal"),
    ("impressions", "Impressions", "fa-eye"),
    ("engagement", "Engagement", "fa-hand-pointer"),
    ("likes", "Likes", "fa-heart"),
    ("comments", "Comments", "fa-comment"),
    ("shares", "Shares", "fa-share"),
    ("saved", "Saves", "fa-bookmark"),
    ("views", "Views", "fa-play"),
]


def _latest_snapshot_ids():
    """One snapshot id per target - the most recent."""
    return (
        select(func.max(SocialAnalyticsSnapshot.id))
        .group_by(SocialAnalyticsSnapshot.target_id)
    )


def _published_targets(period, client_id=None):
    q = (
        SocialPostTarget.query
        .filter(SocialPostTarget.status.in_(["published", "removed"]),
                SocialPostTarget.external_post_id.isnot(None))
    )
    if client_id:
        q = (q.join(SocialPost,
                    SocialPost.id == SocialPostTarget.social_post_id)
              .filter(SocialPost.client_id == client_id))
    if period and not period.get("is_all_time"):
        q = q.filter(
            func.date(SocialPostTarget.updated_at) >= period["start"],
            func.date(SocialPostTarget.updated_at) <= period["end"],
        )
    return q


def build_report(period, client_id=None):
    """Totals, per-channel rows and per-post rows for one window.

    Returns zeroed totals and empty lists when there is nothing yet - the
    screen says so plainly rather than showing invented placeholders.
    """
    targets = _published_targets(period, client_id).all()
    if not targets:
        return _empty()

    by_id = {t.id: t for t in targets}

    snapshots = (
        SocialAnalyticsSnapshot.query
        .filter(SocialAnalyticsSnapshot.id.in_(_latest_snapshot_ids()),
                SocialAnalyticsSnapshot.target_id.in_(list(by_id)))
        .all()
    )

    totals = {key: 0 for key, _, _ in METRICS}
    present = set()
    channels = {}
    posts = []

    for snapshot in snapshots:
        target = by_id.get(snapshot.target_id)
        if target is None:
            continue
        metrics = snapshot.metrics or {}

        row = {"target": target, "metrics": {}, "fetched_at": snapshot.fetched_at}
        for key, _, _ in METRICS:
            value = metrics.get(key)
            if value is None:
                continue
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            totals[key] += value
            present.add(key)
            row["metrics"][key] = value

        posts.append(row)

        account = target.account
        name = account.display_name if account else "Disconnected channel"
        bucket = channels.setdefault(name, {
            "platform": target.platform,
            "posts": 0,
            "metrics": {key: 0 for key, _, _ in METRICS},
        })
        bucket["posts"] += 1
        for key, value in row["metrics"].items():
            bucket["metrics"][key] += value

    posts.sort(key=lambda r: r["metrics"].get("impressions",
                                              r["metrics"].get("reach", 0)),
               reverse=True)

    return {
        "totals": totals,
        # Only tile what the platforms actually reported. A column of
        # zeroes for a metric nobody returns reads as "we got zero reach",
        # which is a different and much worse claim than "not reported".
        "present": present,
        "channels": sorted(channels.items(),
                           key=lambda kv: kv[1]["posts"], reverse=True),
        "posts": posts[:50],
        "post_count": len(targets),
        "measured_count": len(posts),
    }


def _empty():
    return {
        "totals": {key: 0 for key, _, _ in METRICS},
        "present": set(),
        "channels": [],
        "posts": [],
        "post_count": 0,
        "measured_count": 0,
    }
