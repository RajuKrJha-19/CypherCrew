from datetime import datetime

from app.extensions import db


class TaskComment(db.Model):

    __tablename__ = "task_comments"

    id = db.Column(db.Integer, primary_key=True)

    task_id = db.Column(
        db.Integer,
        db.ForeignKey("tasks.id"),
        nullable=False
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False
    )

    parent_id = db.Column(
        db.Integer,
        db.ForeignKey("task_comments.id"),
        nullable=True
    )

    message = db.Column(
        db.Text,
        nullable=False
    )

    is_edited = db.Column(
        db.Boolean,
        default=False
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )

    task = db.relationship(
        "Task",
        backref="comments"
    )

    user = db.relationship(
        "User",
        backref="task_comments"
    )

    replies = db.relationship(
        "TaskComment",
        backref=db.backref(
            "parent",
            remote_side=[id]
        ),
        cascade="all, delete-orphan"
    )


class TaskCommentReaction(db.Model):

    __tablename__ = "task_comment_reactions"

    id = db.Column(db.Integer, primary_key=True)

    comment_id = db.Column(
        db.Integer,
        db.ForeignKey("task_comments.id"),
        nullable=False
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False
    )

    emoji = db.Column(
        db.String(20),
        nullable=False
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    comment = db.relationship(
        "TaskComment",
        backref="reactions"
    )

    user = db.relationship(
        "User"
    )