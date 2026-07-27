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

    #: One of app.utils.roles.ALL_ROLE_VALUES. Deliberately a plain string
    #: with no CHECK constraint: the catalog validates on the way in (see
    #: roles.can_assign_role), and a database constraint would turn adding
    #: a role into a migration. Indexed because the team dashboards and
    #: every people-picker filter on it. 50 rather than 30 - the longest
    #: value, senior_social_media_executive, is 29 characters, and one
    #: character of headroom is not headroom.
    role = db.Column(
        db.String(50),
        nullable=False,
        index=True
    )

    #: foreign_keys is required now that user_permissions points at users
    #: twice - once for the holder, once for whoever granted it.
    permissions = db.relationship(
        "UserPermission",
        backref="user",
        cascade="all, delete-orphan",
        foreign_keys="UserPermission.user_id"
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