from datetime import datetime

from app.extensions import db


class TaskTransferRequest(db.Model):
    """One employee asking another to take over their task.

    Peer-to-peer by design: the recipient accepting is what moves the
    task, with no manager sign-off in between (managers can already
    reassign directly, and are notified when a transfer lands). The
    request is a row rather than a flag on Task because it carries a
    conversation - who asked whom, why, and what they answered - which
    has to survive the transfer to be worth anything afterwards.
    """

    __tablename__ = "task_transfer_requests"

    #: The only state in which a request can be acted on. Everything
    #: else is terminal, which is what keeps a task to at most one live
    #: request (see TaskTransferRequest.pending_for).
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

    id = db.Column(db.Integer, primary_key=True)

    task_id = db.Column(
        db.Integer,
        db.ForeignKey("tasks.id"),
        nullable=False,
        index=True,
    )

    #: Who asked - the task's assignee at the time of asking. Kept even
    #: after the transfer completes, so the timeline can say who handed
    #: the task over rather than just who holds it now.
    from_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
    )

    to_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    status = db.Column(
        db.String(20),
        nullable=False,
        default=PENDING,
        index=True,
    )

    #: Why the requester wants to hand it over.
    message = db.Column(db.Text)

    #: The recipient's reply, most useful when they decline.
    response_message = db.Column(db.Text)

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    responded_at = db.Column(db.DateTime)

    task = db.relationship("Task", backref=db.backref(
        "transfer_requests",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="TaskTransferRequest.created_at.desc()",
    ))

    from_user = db.relationship("User", foreign_keys=[from_user_id])
    to_user = db.relationship("User", foreign_keys=[to_user_id])

    @property
    def is_pending(self):
        return self.status == self.PENDING

    @classmethod
    def pending_for(cls, task_id):
        """The one live request on a task, or None.

        Every route funnels through this rather than filtering inline,
        so "a task has at most one open request" is enforced in one
        place instead of being re-derived (and eventually mis-derived)
        at each call site.
        """
        return cls.query.filter_by(
            task_id=task_id,
            status=cls.PENDING,
        ).first()

    def __repr__(self):
        return (
            f"<TaskTransferRequest task={self.task_id} "
            f"{self.from_user_id}->{self.to_user_id} {self.status}>"
        )
