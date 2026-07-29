"""Who is around, and who is mid-sentence.

Both tables are hot and disposable: a row here is rewritten every time its
user polls. That is exactly why neither lives as a column on `users` - under
Postgres MVCC every write makes a new row version, and putting that churn on
`users` would bloat the one table that every people-picker, dashboard and
permission check in the app already reads.
"""

from datetime import datetime, timedelta

from app.extensions import db


class TeamPresence(db.Model):
    """Last-seen heartbeat, written by the sync poll rather than its own
    endpoint - a heartbeat that costs an extra request per tick is a
    heartbeat that doubles the load of the thing it is measuring."""

    __tablename__ = "teams_presence"

    #: The user IS the row. No surrogate key: there is exactly one presence
    #: record per person and upserting on the primary key is the whole API.
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    last_seen_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, index=True
    )

    #: What the user chose to show: online | away | busy | offline.
    #: "offline" here means "explicitly appear offline"; genuine absence is
    #: derived from last_seen_at instead, so a closed tab needs no write.
    status = db.Column(db.String(10), nullable=False, default="online")

    #: Optional free text ("On leave till Monday", "Heads down").
    status_text = db.Column(db.String(80), nullable=True)

    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user = db.relationship("User")

    def derived_status(self, online_seconds=60, away_seconds=300, now=None):
        """Effective status: an explicit choice wins, otherwise infer it
        from how long ago this row was last touched."""
        if self.status in ("busy", "offline"):
            return self.status
        now = now or datetime.utcnow()
        if not self.last_seen_at:
            return "offline"
        age = now - self.last_seen_at
        if age <= timedelta(seconds=online_seconds):
            return "online"
        if age <= timedelta(seconds=away_seconds):
            return "away"
        return "offline"

    def __repr__(self):
        return f"<TeamPresence u{self.user_id} {self.status}>"


class TeamTyping(db.Model):
    """"X is typing", with an expiry instead of a stop signal.

    A table rather than process memory because the two gunicorn workers
    share nothing - a dict would show the indicator to roughly half the
    team. Expiry rather than an explicit "stopped typing" call because the
    browser that closes mid-sentence never sends one.
    """

    __tablename__ = "teams_typing"

    channel_id = db.Column(
        db.Integer,
        db.ForeignKey("teams_channels.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    #: Short (a few seconds). Refreshed while keys are still landing.
    expires_at = db.Column(db.DateTime, nullable=False, index=True)

    user = db.relationship("User")

    def __repr__(self):
        return f"<TeamTyping c{self.channel_id} u{self.user_id}>"
