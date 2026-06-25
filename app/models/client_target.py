from datetime import datetime

from app.extensions import db


class ClientMonthlyTarget(db.Model):
    __tablename__ = "client_monthly_targets"

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

    id = db.Column(db.Integer, primary_key=True)

    monthly_target_id = db.Column(
        db.Integer,
        db.ForeignKey("client_monthly_targets.id"),
        nullable=False
    )

    service_name = db.Column(db.String(120), nullable=False)

    deliverable_name = db.Column(db.String(150), nullable=False)

    completed_count = db.Column(db.Integer, default=0)

    target_count = db.Column(db.Integer, default=0)

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    monthly_target = db.relationship(
        "ClientMonthlyTarget",
        backref="deliverables"
    )