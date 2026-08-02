from datetime import datetime

from app.extensions import db


class AIUsage(db.Model):
    """One row per AI call - the spend + activity log behind the AI Usage
    screen and the monthly budget cap.

    Deliberately append-only and self-contained: nullable user/client FKs (a
    call may not have either), so a deleted user or client never blocks or
    breaks the log, and the generic test cleanup doesn't need to know it exists.
    """

    __tablename__ = "ai_usage"

    id = db.Column(db.Integer, primary_key=True)

    #: Indexed - every budget/summary query filters on the current month.
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True)
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id", ondelete="SET NULL"),
        nullable=True)

    feature = db.Column(db.String(30), nullable=False)   # caption|alt_text|media_qa
    provider = db.Column(db.String(30))
    model = db.Column(db.String(120))

    input_tokens = db.Column(db.Integer, nullable=False, default=0)
    output_tokens = db.Column(db.Integer, nullable=False, default=0)
    est_cost_usd = db.Column(db.Float, nullable=False, default=0.0)

    status = db.Column(db.String(20), nullable=False, default="ok")  # ok|error

    # For the admin table only (small, paged list). No backrefs - a user/client
    # doesn't need a collection of usage rows hanging off it.
    user = db.relationship("User")
    client = db.relationship("Client")

    def __repr__(self):
        return f"<AIUsage {self.feature} {self.model} ${self.est_cost_usd}>"
