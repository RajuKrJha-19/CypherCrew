from datetime import datetime

from flask_login import UserMixin

from app.extensions import db


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    name = db.Column(
        db.String(120),
        nullable=False
    )

    email = db.Column(
        db.String(150),
        unique=True,
        nullable=False
    )

    phone = db.Column(
        db.String(20)
    )

    password_hash = db.Column(
        db.String(255),
        nullable=False
    )

    role = db.Column(
        db.String(30),
        nullable=False
    )

    permissions = db.relationship(
        "UserPermission",
        backref="user",
        cascade="all, delete-orphan"
    )

    designation = db.Column(
        db.String(120)
    )

    status = db.Column(
        db.String(20),
        default="active"
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    # --- Profile fields (all optional; added additively so existing rows
    #     stay valid). Editable by the account owner on their profile. ---

    #: R2 object key for the profile picture; None -> initials fallback.
    avatar_key = db.Column(
        db.String(255)
    )

    department = db.Column(
        db.String(120)
    )

    location = db.Column(
        db.String(120)
    )

    bio = db.Column(
        db.String(500)
    )

    date_of_birth = db.Column(
        db.Date
    )

    @property
    def initials(self):
        """One- or two-letter fallback avatar from the name/email."""
        source = (self.name or self.email or "?").strip()
        parts = source.split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return source[0].upper() if source else "?"

    def __repr__(self):
        return f"<User {self.email}>"