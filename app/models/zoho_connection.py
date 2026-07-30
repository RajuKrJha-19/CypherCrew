from datetime import datetime

from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db


class ZohoConnection(db.Model):
    """The single org-level Zoho People connection we pull attendance from
    and push check-outs to.

    Unlike a social account (one per Page/channel), attendance is one
    organisation authorising once, so there is normally a single active row.
    The refresh token is stored Fernet-encrypted via app.social.tokens.vault
    (the plaintext never touches the database); the short-lived access token
    is refreshed just before use, exactly like the Google adapter.
    """

    __tablename__ = "zoho_connections"

    id = db.Column(db.Integer, primary_key=True)

    # Data-centre this org lives in (com | in | eu | ...), so a refreshed
    # deployment talks to the right accounts/people hosts.
    dc = db.Column(db.String(10), nullable=False, default="com")

    scopes = db.Column(db.Text, nullable=True)

    # Encrypted secrets + the key version used to encrypt them (rotation).
    token_ciphertext = db.Column(db.Text, nullable=True)
    token_key_version = db.Column(db.Integer, nullable=False, default=1)
    refresh_ciphertext = db.Column(db.Text, nullable=True)
    token_expires_at = db.Column(db.DateTime, nullable=True)

    # active | needs_reauth | revoked
    status = db.Column(db.String(30), nullable=False, default="active")

    connected_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )

    # Org id, portal name, api domain returned at token time, etc.
    meta = db.Column(JSONB, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    connected_by = db.relationship("User")

    def __repr__(self):
        return f"<ZohoConnection {self.status} dc={self.dc}>"
