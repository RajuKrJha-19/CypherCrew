from datetime import datetime
from app.extensions import db


class TaskFeedback(db.Model):
    __tablename__ = "task_feedbacks"

    id = db.Column(db.Integer, primary_key=True)

    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    message = db.Column(db.Text, nullable=False)
    file_name = db.Column(db.String(255))
    file_path = db.Column(db.String(255))
    file_type = db.Column(db.String(50))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    task = db.relationship("Task", backref="feedbacks")
    sender = db.relationship("User", foreign_keys=[sender_id])
    receiver = db.relationship("User", foreign_keys=[receiver_id])