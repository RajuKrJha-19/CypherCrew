from datetime import datetime

from app.extensions import db


class SocialComment(db.Model):
    """A comment on a published post (the Engage inbox). Pulled from the
    platform per published target and cached here so the team can triage,
    reply and mark handled. Our own replies are stored too (is_ours=True)."""

    __tablename__ = "social_comments"

    id = db.Column(db.Integer, primary_key=True)

    target_id = db.Column(
        db.Integer, db.ForeignKey("social_post_targets.id"),
        nullable=False, index=True,
    )
    platform = db.Column(db.String(30), nullable=False)
    external_id = db.Column(db.String(255), nullable=False)
    parent_external_id = db.Column(db.String(255), nullable=True)

    author_name = db.Column(db.String(255), nullable=True)
    author_id = db.Column(db.String(255), nullable=True)
    # Commenter's profile-picture URL where the platform gives us one
    # (Facebook via from{picture}; Instagram's comment API returns none).
    author_pic = db.Column(db.String(500), nullable=True)
    message = db.Column(db.Text, nullable=True)
    created_time = db.Column(db.String(40), nullable=True)  # platform time str

    is_ours = db.Column(db.Boolean, nullable=False, default=False)
    replied = db.Column(db.Boolean, nullable=False, default=False)
    # open | done
    status = db.Column(db.String(20), nullable=False, default="open")

    fetched_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    target = db.relationship("SocialPostTarget")

    __table_args__ = (
        db.UniqueConstraint("platform", "external_id",
                            name="uq_social_comment_platform_external"),
    )

    def __repr__(self):
        return f"<SocialComment {self.platform}:{self.external_id}>"
