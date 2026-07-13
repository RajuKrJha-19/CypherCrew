from datetime import datetime

from app.extensions import db


class TaskFile(db.Model):
    __tablename__ = "task_files"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    task_id = db.Column(
        db.Integer,
        db.ForeignKey("tasks.id"),
        nullable=False,
        index=True
    )

    bucket_name = db.Column(
        db.String(100),
        nullable=False
    )
    
    storage_provider = db.Column(
        db.String(30),
        nullable=False,
        default="r2",
        index=True
    )

    object_key = db.Column(
        db.String(1000),
        nullable=False,
        unique=True
    )

    original_filename = db.Column(
        db.String(255),
        nullable=False
    )

    stored_filename = db.Column(
        db.String(255),
        nullable=False
    )

    mime_type = db.Column(
        db.String(150)
    )

    file_size = db.Column(
        db.BigInteger,
        default=0
    )

    folder_type = db.Column(
        db.String(30),
        nullable=False
    )

    version = db.Column(
        db.Integer,
        default=1,
        nullable=False
    )

    is_final = db.Column(
        db.Boolean,
        default=False,
        nullable=False
    )

    uploaded_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    task = db.relationship(
        "Task",
        backref=db.backref(
            "files",
            lazy=True,
            cascade="all, delete-orphan"
        )
    )

    uploaded_by = db.relationship(
        "User"
    )

    def __repr__(self):
        return (
            f"<TaskFile {self.original_filename}>"
        )