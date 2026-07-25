from datetime import datetime

from app.extensions import db


class SocialOAuthState(db.Model):
    """Short-lived state for an in-flight OAuth handshake. Guards the
    callback against CSRF (the `state` nonce must round-trip) and carries
    the PKCE `code_verifier` where the platform supports PKCE (e.g. Google).
    Rows are single-use and expire quickly.
    """

    __tablename__ = "social_oauth_states"

    id = db.Column(db.Integer, primary_key=True)

    state = db.Column(db.String(128), nullable=False, unique=True, index=True)
    platform = db.Column(db.String(30), nullable=False)
    code_verifier = db.Column(db.String(255), nullable=True)
    redirect_uri = db.Column(db.String(500), nullable=False)

    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<SocialOAuthState {self.platform}:{self.state[:8]}>"
