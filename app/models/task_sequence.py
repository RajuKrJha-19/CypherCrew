from app.extensions import db


class TaskSequence(db.Model):

    __tablename__ = "task_sequences"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    last_code = db.Column(
        db.Integer,
        nullable=False,
        default=1000
    )