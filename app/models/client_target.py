from datetime import datetime

from app.extensions import db


class ClientMonthlyTarget(db.Model):
    __tablename__ = "client_monthly_targets"

    # One target row per client per month - the add-deliverable path
    # get-or-creates on these three, and a duplicate would split a month's
    # deliverables across two rows and undercount the dashboard tally.
    __table_args__ = (
        db.UniqueConstraint(
            "client_id", "month", "year", name="uq_client_month_year"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)

    client_id = db.Column(
        db.Integer,
        db.ForeignKey("clients.id"),
        nullable=False
    )

    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    client = db.relationship(
        "Client",
        backref="monthly_targets"
    )


class ClientDeliverable(db.Model):
    __tablename__ = "client_deliverables"

    __table_args__ = (
        db.CheckConstraint(
            "completed_count >= 0 AND target_count >= 0",
            name="ck_client_deliverables_counts_non_negative",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)

    monthly_target_id = db.Column(
        db.Integer,
        db.ForeignKey("client_monthly_targets.id"),
        nullable=False
    )

    service_name = db.Column(db.String(120), nullable=False)

    deliverable_name = db.Column(db.String(150), nullable=False)

    # NOT NULL with a server-side default and a non-negative CHECK (see the
    # table args below). These were nullable with a Python-only default, and
    # the max(0, ...) clamp lived in two routes - so anything writing outside
    # them could store NULL or a negative, and the client dashboard coalesces
    # NULL to 0, which renders a negative drift as if the counter were simply
    # behind. All writes now go through app/services/deliverables.py; this is
    # the floor under that.
    completed_count = db.Column(
        db.Integer, nullable=False, default=0, server_default="0")

    target_count = db.Column(
        db.Integer, nullable=False, default=0, server_default="0")

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    monthly_target = db.relationship(
        "ClientMonthlyTarget",
        backref="deliverables"
    )