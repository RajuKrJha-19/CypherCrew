from datetime import datetime

from app.extensions import db


class UserPermission(db.Model):
    """One granted permission, for one person.

    A row is the only thing that grants anything (the sole exception being
    the owner, who short-circuits in has_permission and holds no rows at
    all). Role defaults are *materialised* as rows rather than resolved
    from the role at read time, for two reasons: a grant has to be
    revocable individually - which an implicit default is not - and the
    permission queries that pick notification recipients join this table
    directly, so anyone whose access existed only in Python would be
    silently missed.
    """

    __tablename__ = "user_permissions"

    __table_args__ = (
        # The permissions screen used to delete every row and re-insert the
        # submitted set, so a double submit could write the same grant
        # twice. Nothing read duplicates wrongly, but they made "what does
        # this person have" a question with more than one answer.
        db.UniqueConstraint(
            "user_id", "permission_id",
            name="uq_user_permissions_user_permission",
        ),
    )

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False
    )

    permission_id = db.Column(
        db.Integer,
        db.ForeignKey("permissions.id"),
        nullable=False
    )

    #: When and by whom. Nullable because every grant that predates this
    #: column has no honest answer, and inventing one would be worse than
    #: leaving it blank.
    granted_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    granted_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id")
    )

    permission = db.relationship(
        "Permission"
    )

    #: Explicit join - `users` is now reachable from two foreign keys
    #: (the holder and the granter), so SQLAlchemy cannot guess.
    granted_by = db.relationship(
        "User",
        foreign_keys=[granted_by_id],
    )
