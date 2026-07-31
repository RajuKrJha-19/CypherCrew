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


def consume_state(state, expected_by_id=None):
    """Fetch-and-delete a state (single use). Returns the row if it existed,
    had not expired, and - when `expected_by_id` is given - was created by that
    same user.

    Binding to the creator stops an account-injection CSRF: an attacker who
    captures a live (code, state) pair cannot get it accepted in a victim's
    session (which would write the attacker's Page/token into the agency under
    the victim's name). with_for_update makes the single-use atomic so two
    concurrent callbacks with the same state can't both succeed.
    """
    row = (SocialOAuthState.query
           .filter_by(state=state)
           .with_for_update()
           .first())
    if row is None:
        return None
    valid = row.expires_at >= datetime.utcnow()
    if expected_by_id is not None and row.created_by_id != expected_by_id:
        valid = False
    db.session.delete(row)
    db.session.commit()
    return row if valid else None
