from datetime import datetime
from app.extensions import db


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"))

    title = db.Column(db.String(180), nullable=False)
    message = db.Column(db.Text)
    link = db.Column(db.String(255))
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id], backref="notifications")
    actor = db.relationship("User", foreign_keys=[actor_id])
    task = db.relationship("Task")
