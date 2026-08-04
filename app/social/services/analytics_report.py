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
    PublishResult, SocialAccount, SocialAnalyticsSnapshot, SocialPost,
    SocialPostTarget,
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


def _engagement(metrics):
    """A single engagement number for a post: the platform's own 'engagement'
    figure when given (Facebook: post_engaged_users), else the sum of the
    interaction metrics (Instagram/YouTube: likes+comments+shares+saved)."""
    if metrics.get("engagement") is not None:
        try:
            return int(metrics["engagement"])
        except (TypeError, ValueError):
            return 0
    return sum(int(metrics.get(k) or 0)
               for k in ("likes", "comments", "shares", "saved"))


def _reach_base(metrics):
    """Denominator for engagement RATE: reach if reported, else impressions."""
    base = metrics.get("reach")
    if base is None:
        base = metrics.get("impressions")
    try:
        return int(base) if base else 0
    except (TypeError, ValueError):
        return 0


def _eng_rate(engagement, base):
    """Engagement as a % of reach/impressions, or None when we can't measure it
    (no reach reported) - the #1 comparable KPI Buffer/Hootsuite lead with."""
    return round(engagement / base * 100, 1) if base else None


def _latest_snapshot_ids():
    """One snapshot id per target - the most recent."""
    return (
        select(func.max(SocialAnalyticsSnapshot.id))
        .group_by(SocialAnalyticsSnapshot.target_id)
    )


def _published_targets(period, client_id=None, campaign=None):
    q = (
        SocialPostTarget.query
        .filter(SocialPostTarget.status.in_(["published", "removed"]),
                SocialPostTarget.external_post_id.isnot(None))
    )
    if client_id or campaign:
        q = q.join(SocialPost,
                   SocialPost.id == SocialPostTarget.social_post_id)
        if client_id:
            q = q.filter(SocialPost.client_id == client_id)
        if campaign:
            q = q.filter(SocialPost.campaign == campaign)
    if period and not period.get("is_all_time"):
        # Attribute a post to the window by when it was PUBLISHED - not by
        # updated_at, which an analytics re-sync or a permalink/status edit
        # bumps, dragging an old post into a recent window (the exact drift
        # the docstring warns against). published_at is the immutable publish
        # stamp; a target can carry more than one PublishResult (a republish
        # after removal), so its earliest is the canonical publish time.
        # COALESCE to updated_at only as a safety net for any published target
        # somehow lacking a result row - real ones always have one.
        published_at = (
            select(func.min(PublishResult.published_at))
            .where(PublishResult.target_id == SocialPostTarget.id)
            .correlate(SocialPostTarget)
            .scalar_subquery()
        )
        attributed_date = func.date(
            func.coalesce(published_at, SocialPostTarget.updated_at))
        q = q.filter(
            attributed_date >= period["start"],
            attributed_date <= period["end"],
        )
    return q


def build_report(period, client_id=None, campaign=None):
    """Totals, per-channel rows, per-campaign rows and per-post rows for one
    window.

    Returns zeroed totals and empty lists when there is nothing yet - the
    screen says so plainly rather than showing invented placeholders.
    """
    targets = _published_targets(period, client_id, campaign).all()
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
    campaigns = {}
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

        # Engagement + engagement-rate per post (the headline KPI).
        row["engagement"] = _engagement(row["metrics"])
        row["eng_rate"] = _eng_rate(row["engagement"], _reach_base(row["metrics"]))
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

        # Per-campaign rollup: a post's campaign label groups its targets
        # across every platform, so a client sees the whole campaign's reach.
        post = target.post
        camp = post.campaign if post else None
        if camp:
            cb = campaigns.setdefault(camp, {
                "posts": set(),
                "metrics": {key: 0 for key, _, _ in METRICS},
            })
            cb["posts"].add(target.social_post_id)
            for key, value in row["metrics"].items():
                cb["metrics"][key] += value

    posts.sort(key=lambda r: r["metrics"].get("impressions",
                                              r["metrics"].get("reach", 0)),
               reverse=True)

    campaign_rows = sorted(
        ((name, {"posts": len(b["posts"]), "metrics": b["metrics"]})
         for name, b in campaigns.items()),
        key=lambda kv: kv[1]["metrics"].get(
            "impressions", kv[1]["metrics"].get("reach", 0)),
        reverse=True)

    # Headline engagement rate for the whole window + the standout post.
    total_engagement = sum(p["engagement"] for p in posts)
    eng_rate = _eng_rate(total_engagement, _reach_base(totals))
    top_post = max(posts, key=lambda p: p["engagement"]) if posts else None

    return {
        "totals": totals,
        # Only tile what the platforms actually reported. A column of
        # zeroes for a metric nobody returns reads as "we got zero reach",
        # which is a different and much worse claim than "not reported".
        "present": present,
        "total_engagement": total_engagement,
        "eng_rate": eng_rate,
        "top_post": top_post,
        "channels": sorted(channels.items(),
                           key=lambda kv: kv[1]["posts"], reverse=True),
        "campaigns": campaign_rows,
        "posts": posts[:50],
        "post_count": len(targets),
        "measured_count": len(posts),
    }


def _empty():
    return {
        "totals": {key: 0 for key, _, _ in METRICS},
        "present": set(),
        "total_engagement": 0,
        "eng_rate": None,
        "top_post": None,
        "channels": [],
        "campaigns": [],
        "posts": [],
        "post_count": 0,
        "measured_count": 0,
    }
