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

    # ------------------------------------------------------------------
    # Cypher-Teams
    # ------------------------------------------------------------------
    # A meeting used to be a diary entry: a title, a time and a list of
    # names. These columns make it something you can actually join. Every
    # one is nullable or defaulted, so the rows that predate Teams stay
    # valid and the existing readers (calendar, dashboard "Upcoming",
    # meetings list) keep working without a single edit.

    #: Unguessable room identifier, secrets.token_urlsafe(18). The provider
    #: derives its own room name from this rather than from the meeting id,
    #: so knowing that meeting 41 exists tells you nothing about how to
    #: join it. NULL on pre-Teams rows; minted lazily on first join.
    room_key = db.Column(db.String(64), nullable=True)

    duration_minutes = db.Column(
        db.Integer, nullable=False, default=30, server_default="30"
    )

    # scheduled | live | ended | cancelled
    status = db.Column(
        db.String(20), nullable=False,
        default="scheduled", server_default="scheduled",
    )

    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )

    #: Meetings can belong to a channel, which is what lets the meeting card
    #: appear in that conversation and everyone in it join without being
    #: individually invited.
    channel_id = db.Column(
        db.Integer,
        db.ForeignKey("teams_channels.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: Which adapter serves the room. Stored per meeting, not read from
    #: config at join time, so switching the default provider later cannot
    #: strand a meeting that is already in progress.
    provider = db.Column(
        db.String(30), nullable=False,
        default="jitsi", server_default="jitsi",
    )

    started_at = db.Column(db.DateTime, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("room_key", name="uq_meetings_room_key"),
    )

    client = db.relationship("Client")

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    channel = db.relationship("TeamChannel")

    participants = db.relationship(
        "User",
        secondary=meeting_participants,
        backref=db.backref("meetings", lazy="dynamic"),
        lazy="joined"
    )