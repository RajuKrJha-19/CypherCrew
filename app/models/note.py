from datetime import datetime

from app.extensions import db


class Note(db.Model):

    __tablename__ = "notes"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False
    )

    title = db.Column(
        db.String(180),
        default="Untitled Note"
    )

    content = db.Column(
        db.Text,
        default=""
    )

    color = db.Column(
        db.String(30),
        default="white"
    )

    is_pinned = db.Column(
        db.Boolean,
        default=False,
        nullable=False
    )

    is_archived = db.Column(
        db.Boolean,
        default=False,
        nullable=False
    )

    is_deleted = db.Column(
        db.Boolean,
        default=False,
        nullable=False
    )

    reminder_at = db.Column(
        db.DateTime,
        nullable=True
    )

    due_date = db.Column(
        db.DateTime,
        nullable=True
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    user = db.relationship(
        "User",
        backref="notes"
    )

    labels = db.relationship(
        "NoteLabel",
        backref="note",
        cascade="all, delete-orphan"
    )

    attachments = db.relationship(
        "NoteAttachment",
        backref="note",
        cascade="all, delete-orphan"
    )


class NoteLabel(db.Model):

    __tablename__ = "note_labels"

    id = db.Column(db.Integer, primary_key=True)

    note_id = db.Column(
        db.Integer,
        db.ForeignKey("notes.id"),
        nullable=False
    )

    label = db.Column(
        db.String(80),
        nullable=False
    )


class NoteAttachment(db.Model):

    __tablename__ = "note_attachments"

    id = db.Column(db.Integer, primary_key=True)

    note_id = db.Column(
        db.Integer,
        db.ForeignKey("notes.id"),
        nullable=False
    )

    filename = db.Column(
        db.String(255),
        nullable=False
    )

    original_name = db.Column(
        db.String(255)
    )

    uploaded_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )