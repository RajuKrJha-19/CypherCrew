from datetime import datetime

from app.extensions import db


class AttendanceSettings(db.Model):
    """Admin-tunable idle-task alert behaviour. A single row (id=1) - the
    config defaults seed it, and the Attendance admin page edits it, so the
    "buzzer" can be turned off or its timing changed without a redeploy.
    """

    __tablename__ = "attendance_settings"

    id = db.Column(db.Integer, primary_key=True)

    #: Master switch for the whole idle-alert feature. Off => nobody is
    #: nudged (no notification, no buzzer).
    idle_alerts_enabled = db.Column(
        db.Boolean, nullable=False, default=True)

    #: Whether an idle alert plays a distinct buzzer sound (synthesised in the
    #: browser) - separate from the normal notification chime. Off => the
    #: idle notification still appears silently in the bell.
    buzzer_enabled = db.Column(db.Boolean, nullable=False, default=True)
    #: Buzzer loudness, 0-100.
    buzzer_volume = db.Column(db.Integer, nullable=False, default=70)

    #: Minutes after check-in before the first nudge.
    grace_min = db.Column(db.Integer, nullable=False, default=15)
    #: Minutes between repeat nudges while still idle.
    repeat_min = db.Column(db.Integer, nullable=False, default=10)

    #: Whether to loop in a manager when someone stays idle.
    escalate_enabled = db.Column(db.Boolean, nullable=False, default=True)
    #: Consecutive nudges before escalating to a manager.
    escalate_after = db.Column(db.Integer, nullable=False, default=3)

    #: Minutes a "Snooze" suppresses the alert.
    snooze_min = db.Column(db.Integer, nullable=False, default=15)

    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow)
    updated_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True)

    updated_by = db.relationship("User")

    def __repr__(self):
        return f"<AttendanceSettings enabled={self.idle_alerts_enabled}>"
