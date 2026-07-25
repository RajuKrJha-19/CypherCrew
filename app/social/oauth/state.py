"""Single-use OAuth state (CSRF nonce + optional PKCE verifier)."""

import secrets
from datetime import datetime, timedelta

from app.extensions import db
from app.models import SocialOAuthState


_STATE_TTL_MINUTES = 15


def create_state(platform, redirect_uri, created_by_id, pkce=False):
    row = SocialOAuthState(
        state=secrets.token_urlsafe(32),
        platform=platform,
        code_verifier=(secrets.token_urlsafe(48) if pkce else None),
        redirect_uri=redirect_uri,
        created_by_id=created_by_id,
        expires_at=datetime.utcnow() + timedelta(minutes=_STATE_TTL_MINUTES),
    )
    db.session.add(row)
    db.session.commit()
    return row


def consume_state(state):
    """Fetch-and-delete a state (single use). Returns the row if it existed
    and had not expired, else None."""
    row = SocialOAuthState.query.filter_by(state=state).first()
    if row is None:
        return None
    valid = row.expires_at >= datetime.utcnow()
    db.session.delete(row)
    db.session.commit()
    return row if valid else None
