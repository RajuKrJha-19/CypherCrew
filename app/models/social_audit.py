from datetime import datetime

from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db


class SocialAuditLog(db.Model):
    """Engine-level audit trail: connect / disconnect / schedule /
    reschedule / publish / retry / revoke. `actor_id` is nullable for
    system actions (worker, scheduler), matching the TaskActivity
    convention. Task-linked actions ALSO write a TaskActivity row so they
    appear on the task timeline."""

    __tablename__ = "social_audit_logs"

    id = db.Column(db.Integer, primary_key=True)

    actor_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    account_id = db.Column(
        db.Integer, db.ForeignKey("social_accounts.id"), nullable=True
    )
    post_id = db.Column(
        db.Integer, db.ForeignKey("social_posts.id"), nullable=True
    )
    target_id = db.Column(
        db.Integer, db.ForeignKey("social_post_targets.id"), nullable=True
    )

    action = db.Column(db.String(100), nullable=False)
    detail = db.Column(JSONB, nullable=True)

    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, index=True
    )

    actor = db.relationship("User")

    def __repr__(self):
        return f"<SocialAuditLog {self.action}>"


class ContentVersion(db.Model):
    """A JSON snapshot of a post/target's content at a point in time, so
    edits are reversible and auditable (Version History). Written whenever
    content or schedule changes."""

    __tablename__ = "content_versions"

    id = db.Column(db.Integer, primary_key=True)

    social_post_id = db.Column(
        db.Integer, db.ForeignKey("social_posts.id"), nullable=True, index=True
    )
    target_id = db.Column(
        db.Integer, db.ForeignKey("social_post_targets.id"),
        nullable=True, index=True,
    )

    snapshot = db.Column(JSONB, nullable=True)
    edited_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    edited_by = db.relationship("User")

    def __repr__(self):
        return f"<ContentVersion post={self.social_post_id} target={self.target_id}>"
