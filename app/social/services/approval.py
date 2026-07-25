"""ApprovalService - the sole authority that green-lights a publish.

Approval reuses CypherCrew's existing review posture: a post must be
explicitly approved (by someone with approve_tasks / publish_tasks) before
any target can be scheduled or published. Centralizing it here is what
prevents the old bypass where dragging a task to "Published" implied an
external publish.
"""

from datetime import datetime

from app.extensions import db
from app.social.services import audit


class NotApproved(Exception):
    pass


def approve_post(post, approver_id, commit=False):
    post.status = "approved"
    post.approved_by_id = approver_id
    post.approved_at = datetime.utcnow()
    audit.record(
        "approved",
        post_id=post.id,
        actor_id=approver_id,
        task_id=post.task_id,
        message="Approved for publishing",
    )
    if commit:
        db.session.commit()
    return post


def require_approved(post):
    """Guard used by scheduling/publishing. Raises if the post has not been
    approved."""
    if post.status not in ("approved", "scheduled", "publishing",
                           "published", "partially_published"):
        raise NotApproved(
            "This post must be approved before it can be scheduled or published."
        )
