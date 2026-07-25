from datetime import datetime

from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db


class SocialAccount(db.Model):
    """A connected platform account - the OAuth identity we publish AS
    (a Facebook Page, an Instagram business account, a LinkedIn
    organization, a YouTube channel). Access/refresh tokens are stored
    Fernet-encrypted (see app.social.tokens.vault); the plaintext never
    touches the database.
    """

    __tablename__ = "social_accounts"

    id = db.Column(db.Integer, primary_key=True)

    platform = db.Column(db.String(30), nullable=False, index=True)
    external_id = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(255), nullable=False)
    # page | ig_business | system_user | organization | channel
    account_type = db.Column(db.String(30), nullable=False, default="page")

    # Which client this account serves (nullable: agency-owned accounts).
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=True, index=True
    )

    scopes = db.Column(db.Text, nullable=True)

    # Encrypted secrets + key version used to encrypt them (rotation).
    token_ciphertext = db.Column(db.Text, nullable=True)
    token_key_version = db.Column(db.Integer, nullable=False, default=1)
    refresh_ciphertext = db.Column(db.Text, nullable=True)
    token_expires_at = db.Column(db.DateTime, nullable=True)
    refresh_expires_at = db.Column(db.DateTime, nullable=True)

    # active | needs_reauth | revoked
    status = db.Column(db.String(30), nullable=False, default="active")

    connected_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )

    # Platform-specific identifiers (system_user id, ig_business_id, ...).
    meta = db.Column(JSONB, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    client = db.relationship("Client")
    connected_by = db.relationship("User")

    __table_args__ = (
        db.UniqueConstraint(
            "platform", "external_id",
            name="uq_social_account_platform_external",
        ),
    )

    def __repr__(self):
        return f"<SocialAccount {self.platform}:{self.display_name}>"
