from datetime import datetime

from app.extensions import db


class DailyReport(db.Model):
    __tablename__ = "daily_reports"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    employee_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False
    )

    task_id = db.Column(
        db.Integer,
        db.ForeignKey("tasks.id"),
        nullable=True
    )

    report_date = db.Column(
        db.Date,
        nullable=False
    )

    completed_work = db.Column(
        db.Text,
        nullable=False
    )

    hours_worked = db.Column(
        db.Float,
        default=0
    )

    in_progress_work = db.Column(
        db.Text
    )

    issues = db.Column(
        db.Text
    )

    tomorrow_plan = db.Column(
        db.Text
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    employee = db.relationship(
        "User",
        backref="daily_reports"
    )

    task = db.relationship(
        "Task",
        backref="daily_reports"
    )


class ActivityLog(db.Model):
    __tablename__ = "activity_logs"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id")
    )

    action = db.Column(
        db.String(255),
        nullable=False
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    user = db.relationship(
        "User",
        backref="activity_logs"
    )