"""Messages someone bookmarked for later.

Its own table, unlike pinning. The difference is who it is for: a pin is
one channel's shared shortlist and lives on the message, a save is one
person's private list and would need a row per person either way.

Nothing about a save is visible to anyone else - not the fact of it, not
the count. That is the point; people bookmark half-finished thoughts and
things they are annoyed about.
"""

from datetime import datetime

from app.extensions import db


class TeamSavedMessage(db.Model):
    __tablename__ = "teams_saved_messages"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id = db.Column(
        db.Integer,
        db.ForeignKey("teams_messages.id", ondelete="CASCADE"),
        nullable=False,
    )

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User")
    message = db.relationship("TeamMessage")

    __table_args__ = (
        # Makes the toggle idempotent in the database rather than in a
        # read-then-write two fast clicks can interleave through - the same
        # reasoning as the reaction constraint.
        db.UniqueConstraint("user_id", "message_id", name="uq_teams_saved"),
        # Ordered (user, id): the only read is "my saved messages, newest
        # first", and it never filters by message.
        db.Index("ix_teams_saved_user", "user_id", "id"),
    )

    def __repr__(self):
        return f"<TeamSavedMessage u{self.user_id} m{self.message_id}>"
