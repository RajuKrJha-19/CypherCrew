from datetime import datetime

from app.extensions import db


class GoogleReview(db.Model):
    """A Google Business Profile review + our reply state.

    One row per review on a connected GBP location (a SocialAccount). Upserted
    idempotently on (account_id, external_id) each sync, so re-syncing never
    duplicates. Replies are drafted by AI and, by default, posted only after a
    human approves - reply_status carries where each review is in that flow.
    """

    __tablename__ = "google_reviews"

    __table_args__ = (
        db.UniqueConstraint("account_id", "external_id",
                            name="uq_google_review_account_external"),
    )

    id = db.Column(db.Integer, primary_key=True)

    account_id = db.Column(
        db.Integer, db.ForeignKey("social_accounts.id", ondelete="CASCADE"),
        nullable=False, index=True)

    #: Google's stable review identifier / resource name.
    external_id = db.Column(db.String(255), nullable=False)

    reviewer_name = db.Column(db.String(150))
    rating = db.Column(db.Integer)                 # 1..5
    comment = db.Column(db.Text)                    # nullable - star-only reviews
    review_created_at = db.Column(db.DateTime)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)

    #: pending  -> no reply yet (human queue)
    #: drafted  -> an AI draft exists, awaiting a human
    #: posted   -> a reply is live on Google
    #: skipped  -> a human chose not to reply
    reply_status = db.Column(db.String(20), nullable=False, default="pending")
    reply_text = db.Column(db.Text)
    reply_ai_generated = db.Column(db.Boolean, nullable=False, default=False)
    #: True ONLY for a guarded auto-sent reply. Recorded explicitly rather than
    #: inferred from "replied_by_id IS NULL" - a human-approved AI reply whose
    #: poster is later deleted (FK -> NULL) must not retroactively read as auto.
    auto_sent = db.Column(db.Boolean, nullable=False, default=False,
                          server_default=db.false())
    replied_at = db.Column(db.DateTime)
    #: Who posted the reply; NULL means it was auto-sent (guarded auto-reply).
    replied_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True)

    account = db.relationship("SocialAccount")
    replied_by = db.relationship("User")

    def __repr__(self):
        return f"<GoogleReview {self.external_id} {self.rating}star {self.reply_status}>"
