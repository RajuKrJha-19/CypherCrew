from datetime import datetime

from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db


class SocialAnalyticsSnapshot(db.Model):
    """A point-in-time metrics snapshot for a published target (likes,
    reach, views, ...). The AnalyticsSyncService appends one on each pull,
    so performance can be trended over time and later feed the AI
    posting-time model."""

    __tablename__ = "social_analytics"

    id = db.Column(db.Integer, primary_key=True)

    target_id = db.Column(
        db.Integer, db.ForeignKey("social_post_targets.id"),
        nullable=False, index=True,
    )
    external_post_id = db.Column(db.String(255), nullable=True)

    metrics = db.Column(JSONB, nullable=True)
    fetched_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow
    )

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.Index("ix_social_analytics_target_fetched", "target_id", "fetched_at"),
    )

    def __repr__(self):
        return f"<SocialAnalyticsSnapshot target={self.target_id}>"
