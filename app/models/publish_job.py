from datetime import datetime

from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db


class PublishJob(db.Model):
    """The durable queue row + per-target state machine. Workers claim due
    jobs with FOR UPDATE SKIP LOCKED, advance the state, and persist
    `provider_state` (container id / upload session / URN) so a retry
    resumes a multi-step publish instead of duplicating it. `idempotency_key`
    guarantees a target is never published twice for the same schedule.
    """

    __tablename__ = "publish_jobs"

    id = db.Column(db.Integer, primary_key=True)

    target_id = db.Column(
        db.Integer, db.ForeignKey("social_post_targets.id"),
        nullable=False, index=True,
    )

    # queued | claimed | uploading | awaiting_remote | publishing
    # | succeeded | failed | dead
    state = db.Column(db.String(30), nullable=False, default="queued")

    attempts = db.Column(db.Integer, nullable=False, default=0)
    max_attempts = db.Column(db.Integer, nullable=False, default=5)

    next_run_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow
    )
    locked_at = db.Column(db.DateTime, nullable=True)
    locked_by = db.Column(db.String(64), nullable=True)

    idempotency_key = db.Column(db.String(255), nullable=True, unique=True)
    provider_state = db.Column(JSONB, nullable=True)

    priority = db.Column(db.Integer, nullable=False, default=100)
    last_error = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    # A target can carry more than one job over its life: a scheduled job,
    # then a publish-now retry after a failure (each has its own unique
    # idempotency_key). `target.job` is therefore the NEWEST job - ordered
    # explicitly so it is deterministic, not an arbitrary row. Every delete
    # path clears jobs by target_id (see _detach_post_history / remove_target),
    # so the cascade here never has to resolve which of several to remove.
    target = db.relationship(
        "SocialPostTarget",
        backref=db.backref(
            "job", uselist=False,
            order_by="PublishJob.id.desc()",
            cascade="all, delete-orphan",
        ),
    )

    __table_args__ = (
        db.Index("ix_publish_jobs_state_next_run", "state", "next_run_at"),
    )

    def __repr__(self):
        return f"<PublishJob {self.id} target={self.target_id} {self.state}>"


class PublishResult(db.Model):
    """Immutable record of a successful publish (external id + permalink +
    raw response). One row per successful publish; kept for Publishing
    History and audit."""

    __tablename__ = "publish_results"

    id = db.Column(db.Integer, primary_key=True)

    target_id = db.Column(
        db.Integer, db.ForeignKey("social_post_targets.id"),
        nullable=False, index=True,
    )

    external_post_id = db.Column(db.String(255), nullable=True)
    permalink = db.Column(db.String(500), nullable=True)
    published_at = db.Column(db.DateTime, nullable=True)
    raw_response = db.Column(JSONB, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<PublishResult target={self.target_id} {self.external_post_id}>"
