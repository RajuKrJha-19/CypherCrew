from datetime import datetime

from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db


class AICheck(db.Model):
    """One AI media-QA pass over a submitted deliverable file.

    Advisory only - it never blocks a workflow. Mirrors TaskFeedback (a record
    hanging off the work, keyed to the file it reviewed), so the generic task-
    children cleanup already covers it via the task_files -> tasks chain.
    """

    __tablename__ = "ai_checks"

    id = db.Column(db.Integer, primary_key=True)

    # ON DELETE CASCADE: a check is meaningless once its file is gone, so it
    # dies with the file - both in the app's delete-file route and in the test
    # cleanup, without either needing to know this table exists.
    task_file_id = db.Column(
        db.Integer,
        db.ForeignKey("task_files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: clean (no findings worse than info) | flagged (>=1 warning/error) |
    #: error (the provider failed - the row records that we tried).
    status = db.Column(db.String(20), nullable=False, default="clean")

    #: Which model produced this pass, for provenance ("gemini-2.5-pro",
    #: "simulation", ...). Never a key - just the model id.
    model = db.Column(db.String(120))

    #: [{"severity": "info|warning|error", "category": str, "message": str}]
    findings = db.Column(JSONB, nullable=False, default=list)

    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
    )

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # passive_deletes so the ORM leaves the row removal to the DB's ON DELETE
    # CASCADE above, instead of trying to NULL the (NOT NULL) FK on delete.
    task_file = db.relationship(
        "TaskFile", backref=db.backref("ai_checks", passive_deletes=True))

    def __repr__(self):
        return f"<AICheck file={self.task_file_id} {self.status}>"
