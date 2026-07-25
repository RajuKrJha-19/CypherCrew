"""Engine health/observability - a single snapshot ops can read to answer
"is the publishing engine healthy?": queue depth, in-flight, dead-letter
count, accounts needing re-auth, and scheduled/failed targets. Cheap
aggregate queries; safe to call any time.
"""

from sqlalchemy import func

from app.extensions import db
from app.models import PublishJob, SocialPostTarget, SocialAccount


#: Job states considered "actively moving through" the pipeline.
_IN_FLIGHT = ("claimed", "uploading", "awaiting_remote", "publishing")


def engine_status():
    job_counts = dict(
        db.session.query(PublishJob.state, func.count())
        .group_by(PublishJob.state)
        .all()
    )

    return {
        "jobs": {
            "queued": job_counts.get("queued", 0),
            "in_flight": sum(job_counts.get(s, 0) for s in _IN_FLIGHT),
            "succeeded": job_counts.get("succeeded", 0),
            "failed": job_counts.get("failed", 0),
            "dead": job_counts.get("dead", 0),
        },
        "accounts": {
            "active": SocialAccount.query.filter_by(status="active").count(),
            "needs_reauth": SocialAccount.query.filter_by(
                status="needs_reauth").count(),
            "revoked": SocialAccount.query.filter_by(status="revoked").count(),
        },
        "targets": {
            "scheduled": SocialPostTarget.query.filter_by(
                status="scheduled").count(),
            "publishing": SocialPostTarget.query.filter_by(
                status="publishing").count(),
            "published": SocialPostTarget.query.filter_by(
                status="published").count(),
            "failed": SocialPostTarget.query.filter_by(status="failed").count(),
        },
    }


def needs_attention():
    """A quick boolean-ish summary for badges/alerts."""
    status = engine_status()
    return {
        "dead_jobs": status["jobs"]["dead"] + status["jobs"]["failed"],
        "accounts_need_reauth": status["accounts"]["needs_reauth"],
        "failed_targets": status["targets"]["failed"],
    }
