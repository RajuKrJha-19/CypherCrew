from datetime import datetime

from app.extensions import db


class AttendanceSession(db.Model):
    """One check-in/out span for a user. The source of truth for "is this
    person on the clock right now": a row whose check_out_at IS NULL is an
    open session (currently checked in).

    Multiple rows per user per day are expected (people punch in and out
    across breaks). The idle-alert bookkeeping lives on the row itself so it
    resets automatically on the next check-in - no separate transient table.
    """

    __tablename__ = "attendance_sessions"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )

    # zoho | software - where this span was recorded.
    source = db.Column(db.String(20), nullable=False, default="software")

    check_in_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # NULL => still checked in.
    check_out_at = db.Column(db.DateTime, nullable=True)

    # Zoho attendance entry id, so a re-sync updates the same span instead of
    # opening a duplicate. NULL for software-sourced (app-native) sessions.
    zoho_entry_id = db.Column(db.String(64), nullable=True, index=True)

    # For a zoho session whose local check-out has not yet been written back
    # to Zoho (the API call failed): the worker retries these.
    checkout_pending_zoho = db.Column(
        db.Boolean, nullable=False, default=False
    )

    last_synced_at = db.Column(db.DateTime, nullable=True)

    # --- Idle-task alert bookkeeping (only meaningful while open) ---
    #: Suppress idle alerts until this time (the "Snooze" button).
    snooze_until = db.Column(db.DateTime, nullable=True)
    #: When we last nudged this user, for the repeat-interval guard.
    last_idle_alert_at = db.Column(db.DateTime, nullable=True)
    #: How many consecutive idle nudges this span has drawn (drives
    #: manager escalation).
    idle_alert_count = db.Column(db.Integer, nullable=False, default=0)
    #: When we last looped in a manager, so escalation fires once per window.
    last_escalated_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User")

    __table_args__ = (
        # At most one open session per user. A partial unique index is the
        # database-level guarantee behind "you are either checked in or not".
        db.Index(
            "uq_attendance_open_per_user",
            "user_id",
            unique=True,
            postgresql_where=db.text("check_out_at IS NULL"),
        ),
        db.Index("ix_attendance_user_checkin", "user_id", "check_in_at"),
    )

    @property
    def is_open(self):
        return self.check_out_at is None

    def __repr__(self):
        state = "open" if self.is_open else "closed"
        return f"<AttendanceSession u{self.user_id} {self.source} {state}>"
