"""OAuthManager - drives the connect handshake through the right provider.

start()  -> returns the consent URL to redirect the user to.
finish() -> validates state, exchanges the code, and returns the token
            bundle + the publishable accounts it unlocked. Persisting the
            accounts (encrypting tokens) is the caller's step via
            AccountManager, so this layer stays free of storage concerns.
"""

from app.social.registry import get_provider
from app.social.oauth import state as state_store
from app.social.errors import PermanentError


class OAuthManager:

    @staticmethod
    def start(platform, redirect_uri, created_by_id, pkce=False):
        provider = get_provider(platform)
        if provider is None:
            raise PermanentError(f"No publisher enabled for {platform}")
        row = state_store.create_state(
            platform, redirect_uri, created_by_id, pkce=pkce
        )
        return provider.build_oauth_url(row.state, redirect_uri)

    @staticmethod
    def finish(platform, code, state):
        row = state_store.consume_state(state)
        if row is None or row.platform != platform:
            raise PermanentError("Invalid or expired OAuth state.")

        provider = get_provider(platform)
        if provider is None:
            raise PermanentError(f"No publisher enabled for {platform}")

        bundle = provider.exchange_code(code, row.code_verifier, row.redirect_uri)
        accounts = provider.list_publishable_accounts(bundle.access_token)
        return bundle, accounts
