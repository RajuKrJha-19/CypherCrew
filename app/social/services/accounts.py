"""AccountManager - connect / list / health / disconnect of SocialAccounts.

The ONLY place tokens cross the vault boundary: tokens are encrypted here
before persistence and decrypted here (never elsewhere, never logged).
"""

from datetime import datetime

from app.extensions import db
from app.models import SocialAccount
from app.social.dto import AccountInfo, TokenBundle
from app.social.tokens.vault import get_vault


class AccountManager:

    @staticmethod
    def list_accounts(client_id=None, platform=None, include_revoked=False):
        q = SocialAccount.query
        if client_id is not None:
            q = q.filter(SocialAccount.client_id == client_id)
        if platform:
            q = q.filter(SocialAccount.platform == platform)
        if not include_revoked:
            q = q.filter(SocialAccount.status != "revoked")
        return q.order_by(SocialAccount.platform, SocialAccount.display_name).all()

    @staticmethod
    def upsert_from_oauth(platform, info: AccountInfo, bundle: TokenBundle,
                          connected_by_id, client_id=None):
        """Create or refresh a connected account from an OAuth result.
        Encrypts tokens via the vault (raises VaultDisabled if no key)."""
        vault = get_vault()

        acct = SocialAccount.query.filter_by(
            platform=platform, external_id=info.external_id
        ).first()
        if acct is None:
            acct = SocialAccount(platform=platform, external_id=info.external_id)
            db.session.add(acct)

        acct.display_name = info.display_name
        acct.account_type = info.account_type
        if client_id is not None:
            acct.client_id = client_id
        acct.scopes = bundle.scopes
        # Prefer a per-account token when the platform issues one (e.g. a
        # Meta Page token); otherwise use the handshake's token.
        access_token = info.access_token or bundle.access_token
        acct.token_ciphertext = vault.encrypt(access_token)
        acct.token_key_version = vault.version
        acct.refresh_ciphertext = (
            vault.encrypt(bundle.refresh_token) if bundle.refresh_token else None
        )
        acct.token_expires_at = info.token_expires_at or bundle.token_expires_at
        acct.refresh_expires_at = bundle.refresh_expires_at
        acct.status = "active"
        acct.connected_by_id = connected_by_id
        acct.meta = {**(acct.meta or {}), **(info.meta or {}), **(bundle.meta or {})}
        return acct

    @staticmethod
    def access_token(account) -> str:
        """Decrypt and return the account's access token."""
        return get_vault().decrypt(account.token_ciphertext)

    @staticmethod
    def refresh_token_value(account):
        if not account.refresh_ciphertext:
            return None
        return get_vault().decrypt(account.refresh_ciphertext)

    @staticmethod
    def store_refreshed(account, bundle: TokenBundle):
        vault = get_vault()
        account.token_ciphertext = vault.encrypt(bundle.access_token)
        account.token_key_version = vault.version
        if bundle.refresh_token:
            account.refresh_ciphertext = vault.encrypt(bundle.refresh_token)
        account.token_expires_at = bundle.token_expires_at
        if bundle.refresh_expires_at:
            account.refresh_expires_at = bundle.refresh_expires_at
        account.status = "active"
        account.updated_at = datetime.utcnow()

    @staticmethod
    def mark_needs_reauth(account, reason=None):
        account.status = "needs_reauth"
        account.updated_at = datetime.utcnow()

    @staticmethod
    def disconnect(account):
        """Revoke locally and wipe stored secrets."""
        account.status = "revoked"
        account.token_ciphertext = None
        account.refresh_ciphertext = None
        account.updated_at = datetime.utcnow()
