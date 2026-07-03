from datetime import datetime

from app.extensions import db


meeting_participants = db.Table(
    "meeting_participants",

    db.Column(
        "meeting_id",
        db.Integer,
        db.ForeignKey("meetings.id", ondelete="CASCADE"),
        primary_key=True
    ),

    db.Column(
        "user_id",
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True
    )
)


class Meeting(db.Model):

    __tablename__ = "meetings"

    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(150), nullable=False)

    client_id = db.Column(
        db.Integer,
        db.ForeignKey("clients.id"),
        nullable=True
    )

    meeting_date = db.Column(
        db.DateTime,
        nullable=False
    )

    agenda = db.Column(db.Text)

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    client = db.relationship("Client")

    participants = db.relationship(
        "User",
        secondary=meeting_participants,
        backref=db.backref("meetings", lazy="dynamic"),
        lazy="joined"
    )