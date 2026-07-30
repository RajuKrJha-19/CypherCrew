"""Zoho People connect flow (real mode).

Reuses the social OAuth state store (single-use CSRF nonce) and the token
vault, but persists into the dedicated zoho_connections table rather than
social_accounts - attendance is one org connection, not many channels.
"""

from datetime import datetime

from app.attendance import zoho_client
from app.extensions import db
from app.models import ZohoConnection
from app.social.oauth.state import consume_state, create_state
from app.social.tokens.vault import get_vault

PLATFORM = "zoho_people"


def start_connect(redirect_uri, created_by_id):
    """Create a state row and return the Zoho consent URL."""
    state = create_state(PLATFORM, redirect_uri, created_by_id)
    return zoho_client.build_oauth_url(state.state, redirect_uri)


def finish_connect(code, state, created_by_id):
    """Validate state, exchange the code, and upsert the org connection.
    Returns the ZohoConnection. Raises on an invalid state or Zoho error."""
    row = consume_state(state)
    if row is None or row.platform != PLATFORM:
        raise ValueError("This connection link has expired. Please try again.")

    bundle = zoho_client.exchange_code(code, row.redirect_uri)
    vault = get_vault()

    from flask import current_app
    conn = ZohoConnection.query.filter_by(status="active").first()
    if conn is None:
        conn = ZohoConnection()
        db.session.add(conn)

    conn.dc = current_app.config.get("ZOHO_DC", "com")
    conn.scopes = current_app.config.get("ZOHO_SCOPES")
    conn.token_ciphertext = vault.encrypt(bundle["access_token"]) \
        if bundle.get("access_token") else None
    conn.refresh_ciphertext = vault.encrypt(bundle["refresh_token"])
    conn.token_key_version = vault.version
    conn.token_expires_at = bundle.get("expires_at")
    conn.status = "active"
    conn.connected_by_id = created_by_id
    conn.meta = bundle.get("meta")
    conn.updated_at = datetime.utcnow()
    db.session.commit()
    return conn


def disconnect():
    """Revoke the active org connection (keeps the row for audit)."""
    conn = ZohoConnection.query.filter_by(status="active").first()
    if conn is not None:
        conn.status = "revoked"
        conn.token_ciphertext = None
        conn.refresh_ciphertext = None
        db.session.commit()
    return conn
