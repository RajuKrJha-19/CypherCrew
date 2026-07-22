from datetime import datetime
from app.extensions import db


task_visibility = db.Table(
    "task_visibility",
    db.Column("task_id", db.Integer, db.ForeignKey("tasks.id"), primary_key=True),
    db.Column("user_id", db.Integer, db.ForeignKey("users.id"), primary_key=True)
)


class Task(db.Model):
    __tablename__ = "tasks"

    id = db.Column(db.Integer, primary_key=True)
    task_code = db.Column(
    db.Integer,
    unique=True,
    nullable=True
)

    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)

    client_id = db.Column(
        db.Integer,
        db.ForeignKey("clients.id"),
        nullable=False
    )

    deliverable_id = db.Column(
        db.Integer,
        db.ForeignKey("client_deliverables.id"),
        nullable=False
    )

    assigned_to_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id")
    )

    priority = db.Column(
        db.String(20),
        default="Medium"
    )

    deadline = db.Column(db.DateTime)

    status = db.Column(
        db.String(30),
        default="Pending"
    )

    quantity = db.Column(
        db.Float,
        default=1
    )

    estimated_time = db.Column(
        db.Float,
        default=1
    )

    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id")
    )

    # ===========================
    # Employee Completion
    # ===========================

    employee_completed = db.Column(
        db.Boolean,
        default=False,
        nullable=False
    )

    employee_completed_at = db.Column(
        db.DateTime,
        nullable=True
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    client = db.relationship("Client")

    deliverable = db.relationship("ClientDeliverable")

    assigned_to = db.relationship(
        "User",
        foreign_keys=[assigned_to_id]
    )

    created_by = db.relationship(
        "User",
        foreign_keys=[created_by_id]
    )

    visible_to = db.relationship(
        "User",
        secondary=task_visibility,
        backref="visible_tasks"
    )

    # ===========================
    # Time Tracking
    # ===========================

    worked_seconds = db.Column(
        db.Integer,
        default=0
    )

    timer_started_at = db.Column(
        db.DateTime,
        nullable=True
    )

    started_at = db.Column(
        db.DateTime,
        nullable=True
    )

    completed_at = db.Column(
        db.DateTime,
        nullable=True
    )

    # ===========================
    # Status Duration
    # ===========================

    pending_seconds = db.Column(
        db.Integer,
        default=0
    )

    in_progress_seconds = db.Column(
        db.Integer,
        default=0
    )

    # Named hold_seconds in the DB but it has always accumulated
    # time spent in "Paused"; mapped to a truthful attribute name
    # so it cannot be confused with the separate On Hold bucket.
    paused_seconds = db.Column(
        "hold_seconds",
        db.Integer,
        default=0
    )

    on_hold_seconds = db.Column(
        db.Integer,
        default=0
    )

    # already present in the DB as NOT NULL DEFAULT 0
    void_seconds = db.Column(
        db.Integer,
        nullable=False,
        server_default="0",
        default=0
    )

    core_review_seconds = db.Column(
        db.Integer,
        default=0
    )

    client_review_seconds = db.Column(
        db.Integer,
        default=0
    )

    published_seconds = db.Column(
        db.Integer,
        default=0
    )

    # ===========================
    # On Hold / Void context
    #
    # Both statuses stop the work for a reason that lives outside
    # the team, so the reason has to travel with the task - without
    # it nobody can tell why a task stalled weeks later.
    # ===========================

    hold_reason = db.Column(
        db.Text,
        nullable=True
    )

    held_at = db.Column(
        db.DateTime,
        nullable=True
    )

    held_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id")
    )

    void_reason = db.Column(
        db.Text,
        nullable=True
    )

    voided_at = db.Column(
        db.DateTime,
        nullable=True
    )

    voided_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id")
    )

    held_by = db.relationship(
        "User",
        foreign_keys=[held_by_id]
    )

    voided_by = db.relationship(
        "User",
        foreign_keys=[voided_by_id]
    )

    status_started_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )
    status_changed_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )