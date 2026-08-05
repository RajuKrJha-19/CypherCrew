from datetime import datetime

from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db


class BackgroundJob(db.Model):
    """A user-triggered background action (Fetch comments, Run auto-reply, ...)
    that runs off the request thread, so the person who started it can SEE it
    running and what it did instead of staring at a spinner or wondering whether
    it worked. One row per run; the Activity/Status screen reads these newest
    first. Deliberately lightweight (no external queue) - a daemon thread writes
    the outcome back here.
    """

    __tablename__ = "background_jobs"

    id = db.Column(db.Integer, primary_key=True)

    #: What ran - e.g. "fetch_comments", "auto_reply". Free string so a new job
    #: type needs no migration; the UI maps known kinds to a label + icon.
    kind = db.Column(db.String(40), nullable=False)

    #: The client this run was scoped to (None = all clients / not scoped).
    #: SET NULL so deleting a client never trips over an old status row.
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id", ondelete="SET NULL"),
        nullable=True)

    #: running -> done | failed. Never blocks anything; purely informational.
    status = db.Column(db.String(12), nullable=False, default="running")

    #: Human one-liner shown in the list ("Fetched 4 new comment(s)").
    message = db.Column(db.String(300), nullable=True)

    #: Structured counts the outcome carried, for anyone who wants detail.
    result = db.Column(JSONB, nullable=True)

    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)

    started_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    client = db.relationship("Client")
    started_by = db.relationship("User")

    @property
    def duration_seconds(self):
        end = self.finished_at or datetime.utcnow()
        return max(0, int((end - self.started_at).total_seconds()))

    def __repr__(self):
        return f"<BackgroundJob {self.id} {self.kind} {self.status}>"
