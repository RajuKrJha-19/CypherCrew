from datetime import datetime

from app.extensions import db


class PlatformRateBudget(db.Model):
    """Rolling-window publish accounting per account, so the queue never
    breaches a platform's publish cap (e.g. Instagram's 100 posts / 24h).
    The rate gate reads/increments the current window; over-budget jobs get
    their next_run_at pushed past the window instead of failing."""

    __tablename__ = "platform_rate_budgets"

    id = db.Column(db.Integer, primary_key=True)

    social_account_id = db.Column(
        db.Integer, db.ForeignKey("social_accounts.id"),
        nullable=False, index=True,
    )
    # Window label, e.g. "24h" - a single account may track more than one.
    rate_window = db.Column(db.String(20), nullable=False, default="24h")
    window_start = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow
    )
    used_count = db.Column(db.Integer, nullable=False, default=0)

    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "social_account_id", "rate_window",
            name="uq_rate_budget_account_window",
        ),
    )

    def __repr__(self):
        return (
            f"<PlatformRateBudget acct={self.social_account_id} "
            f"{self.rate_window} {self.used_count}>"
        )
