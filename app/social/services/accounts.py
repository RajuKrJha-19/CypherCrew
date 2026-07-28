"""AccountManager - connect / list / health / disconnect of SocialAccounts.

The ONLY place tokens cross the vault boundary: tokens are encrypted here
before persistence and decrypted here (never elsewhere, never logged).
"""

from datetime import datetime, timedelta

from flask import current_app

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
    def access_token(account, refresh_margin_seconds=300) -> str:
        """Decrypt the account's access token, renewing it first if it is
        about to expire.

        The scheduled sweep in tokens/refresh.py is not enough on its own
        for Google, whose access tokens live one hour: a post scheduled for
        three hours' time would publish with a token that died two hours
        ago. This is the one place that knows a token is about to be USED,
        so it is where the check belongs.

        Meta is unaffected - its Page tokens carry no expiry, so the guard
        below is skipped entirely and nothing extra is called.
        """
        expires_at = account.token_expires_at
        if expires_at is not None:
            due = datetime.utcnow() + timedelta(seconds=refresh_margin_seconds)
            if expires_at <= due:
                AccountManager._refresh_now(account)

        return get_vault().decrypt(account.token_ciphertext)

    @staticmethod
    def _refresh_now(account):
        """Best-effort in-line refresh.

        A failure here must not raise: the stored token may still have
        minutes left on it, and letting the publish attempt proceed gives a
        real platform error to classify rather than an opaque crash from
        the token layer. A genuinely dead token comes back as AuthError
        from the provider, which already flips the account to needs_reauth.
        """
        from app.social.registry import get_provider

        provider = get_provider(account.platform)
        if provider is None:
            return
        try:
            bundle = provider.refresh_token(account)
        except Exception:  # noqa: BLE001 - see docstring
            current_app.logger.warning(
                "in-line token refresh failed for account=%s platform=%s",
                account.id, account.platform, exc_info=True)
            return
        if bundle is None:
            return
        AccountManager.store_refreshed(account, bundle)
        db.session.commit()

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
