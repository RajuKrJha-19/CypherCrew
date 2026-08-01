import hashlib
from datetime import datetime

from flask_login import UserMixin

from app.extensions import db


def password_fingerprint(password_hash):
    """A short, stable digest of a password hash.

    Lives here rather than in app.routes.auth because both the session
    identity (User.get_id) and the reset-token payload need it, and models
    cannot import routes. Truncated to 16 hex characters: long enough that
    two live hashes will not collide, short enough to sit in a cookie.
    """
    return hashlib.sha256(
        (password_hash or "").encode()
    ).hexdigest()[:16]


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

    # --- Attendance (Zoho People bridge) -----------------------------------
    #: Where this person's attendance comes from. 'zoho' users punch in on
    #: Zoho People and their sessions are synced in; 'software' users (e.g.
    #: interns not on Zoho) check in/out directly in this app. Additive +
    #: nullable so existing rows stay valid; an admin sets it on the user
    #: edit screen. Effective default is 'zoho' (see attendance.source_of).
    checkin_source = db.Column(
        db.String(20)
    )

    #: Zoho People employee id (erecno / empId), resolved by email on first
    #: sync and cached so the write-back checkout call needs no extra lookup.
    zoho_employee_id = db.Column(
        db.String(64),
        index=True
    )

    @property
    def is_active(self):
        """Flask-Login uses this to refuse a deactivated account at login.
        Per-request rejection of an ALREADY-open session is enforced in
        auth.load_user (which returns None for a non-active user) - overriding
        is_active alone does not, because login_required checks is_authenticated.
        """
        return self.status == "active"

    def get_id(self):
        """Session identity: the user id, bound to the current password.

        Flask-Login stores whatever this returns in the session cookie AND in
        the remember-me cookie, then hands it back to the user_loader. Mixing
        the password fingerprint in is what makes changing a password end
        every other open session.

        Without it, Flask-Login's identity is the bare user id, so "reset the
        password" - the standard response to a stolen laptop or a phished
        cookie - left the attacker signed in indefinitely; with
        WTF_CSRF_TIME_LIMIT unset and no PERMANENT_SESSION_LIFETIME, that
        session never aged out on its own either.

        Deliberately not a separate session-epoch column: the password hash is
        already the thing that changes on every event that should invalidate a
        session, so deriving from it needs no migration and cannot drift out
        of step with the password itself.
        """
        return "%s|%s" % (self.id, password_fingerprint(self.password_hash))

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