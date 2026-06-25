from datetime import datetime
from app.extensions import db


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    client_name = db.Column(
        db.String(150),
        nullable=False
    )

    company_name = db.Column(
        db.String(150)
    )

    phone = db.Column(
        db.String(20)
    )

    email = db.Column(
        db.String(150)
    )

    industry = db.Column(
        db.String(100)
    )

    assigned_manager_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id")
    )

    status = db.Column(
        db.String(20),
        default="active"
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    assigned_manager = db.relationship(
        "User"
    )