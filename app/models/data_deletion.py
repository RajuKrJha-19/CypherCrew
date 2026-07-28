"""Record of a user-data deletion request.

Meta requires an app that touches platform user data to offer a way to
have that data deleted, and to give the person back a status URL and a
confirmation code they can quote. That contract only works if the request
survives the HTTP call, so each one is recorded here: what was asked for,
what was actually removed, and when.

Rows are kept AFTER the deletion completes - they are the audit trail
proving the request was honoured, and they deliberately hold no personal
data beyond the platform-scoped id that was asked about.
"""

from datetime import datetime
from secrets import token_hex

from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db


class DataDeletionRequest(db.Model):
    __tablename__ = "data_deletion_requests"

    #: Meta's signed deletion callback (POST from Facebook).
    SOURCE_META_CALLBACK = "meta_callback"
    #: Somebody filled in the form on the data-deletion page.
    SOURCE_WEB_FORM = "web_form"

    STATUS_RECEIVED = "received"
    STATUS_COMPLETED = "completed"
    STATUS_NOTHING_TO_DELETE = "nothing_to_delete"
    STATUS_MANUAL_REVIEW = "manual_review"

    id = db.Column(db.Integer, primary_key=True)

    #: Quoted back to the requester and used to look the request up. Unique
    #: and unguessable, because it is the only thing protecting the status
    #: page from being enumerated.
    confirmation_code = db.Column(
        db.String(40), nullable=False, unique=True, index=True
    )

    source = db.Column(db.String(30), nullable=False)
    platform = db.Column(db.String(30), nullable=True)

    #: The app-scoped user id Meta sent, or whatever identifier the web
    #: form was given. Never an email/password - just the handle needed to
    #: find the data.
    external_user_id = db.Column(db.String(255), nullable=True, index=True)

    status = db.Column(
        db.String(30), nullable=False, default=STATUS_RECEIVED, index=True
    )

    #: What was actually removed, e.g. {"accounts": 2, "comments": 40}.
    #: Counts only - never the deleted content itself.
    deleted = db.Column(JSONB, nullable=True)
    note = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    @staticmethod
    def new_code():
        """A short, unguessable, human-quotable code."""
        return token_hex(8)

    def __repr__(self):
        return f"<DataDeletionRequest {self.confirmation_code} {self.status}>"
