"""OAuthManager - drives the connect handshake through the right provider.

start()      -> returns the consent URL to redirect the user to.
finish()     -> validates state, exchanges the code, and returns the token
                bundle + the publishable accounts for that ONE platform.
finish_all() -> same, but also discovers the sibling platforms that share
                the same OAuth consent (the Meta family: one Facebook login
                unlocks both Facebook Pages and their linked Instagram
                accounts). Returns (bundle, [(platform, AccountInfo)], empty)
                where `empty` lists the group platforms that yielded nothing.

Persisting the accounts (encrypting tokens) is the caller's step via
AccountManager, so this layer stays free of storage concerns.
"""

from flask import current_app

from app.social.registry import get_provider, registry
from app.social.oauth import state as state_store
from app.social.errors import PermanentError, SocialError


class OAuthManager:

    @staticmethod
    def start(platform, redirect_uri, created_by_id, pkce=False):
        provider = get_provider(platform)
        if provider is None:
            raise PermanentError(f"No publisher enabled for {platform}")
        row = state_store.create_state(
            platform, redirect_uri, created_by_id, pkce=pkce
        )
        # A provider in a shared-consent group (Meta) requests the union of
        # the group's scopes so one login connects the whole family.
        connect_scopes = getattr(provider, "connect_scopes", None)
        scopes = connect_scopes() if connect_scopes else None
        if scopes is not None:
            return provider.build_oauth_url(row.state, redirect_uri, scopes=scopes)
        return provider.build_oauth_url(row.state, redirect_uri)

    # -- Handshake completion ---------------------------------------------

    @staticmethod
    def _exchange(platform, code, state):
        """Validate state + exchange the code once. Returns (provider,
        bundle). Logs each step (never the token)."""
        log = current_app.logger

        row = state_store.consume_state(state)
        if row is None or row.platform != platform:
            log.warning("[oauth:%s] state validation FAILED (missing/expired)",
                        platform)
            raise PermanentError("Invalid or expired OAuth state.")
        log.info("[oauth:%s] 1/4 state validated; exchanging authorization code",
                 platform)

        provider = get_provider(platform)
        if provider is None:
            raise PermanentError(f"No publisher enabled for {platform}")

        bundle = provider.exchange_code(code, row.code_verifier, row.redirect_uri)
        log.info("[oauth:%s] 2/4 code -> long-lived token exchanged; scopes "
                 "validated", platform)
        return provider, bundle

    @staticmethod
    def finish(platform, code, state):
        """Single-platform completion (kept for direct callers/tests)."""
        provider, bundle = OAuthManager._exchange(platform, code, state)
        accounts = provider.list_publishable_accounts(bundle.access_token)
        current_app.logger.info(
            "[oauth:%s] 3/4 discovery returned %d publishable account(s)",
            platform, len(accounts))
        return bundle, accounts

    @staticmethod
    def _group_members(platform):
        """The platforms that share `platform`'s OAuth consent, primary
        first. A provider with no connect_group is a group of one."""
        provider = get_provider(platform)
        group = getattr(provider, "connect_group", None)
        if not group:
            return [platform]
        members = [platform]
        for key, prov in registry.all().items():
            if key != platform and getattr(prov, "connect_group", None) == group:
                members.append(key)
        return members

    @staticmethod
    def finish_all(platform, code, state):
        """Complete the handshake AND discover every sibling platform that
        shares this consent. One Facebook login therefore returns both the
        Facebook Pages and the Instagram Business accounts linked to them.

        Returns (bundle, results, empty):
          results -> list of (platform_key, AccountInfo) to persist
          empty   -> group platform keys that yielded no publishable account
                     (so the caller can explain *why*, e.g. no IG linked).
        """
        log = current_app.logger
        provider, bundle = OAuthManager._exchange(platform, code, state)

        results, empty = [], []
        for key in OAuthManager._group_members(platform):
            sibling = get_provider(key)
            if sibling is None:
                continue
            try:
                accounts = sibling.list_publishable_accounts(bundle.access_token)
            except SocialError as exc:
                # The primary platform's failure is fatal; a sibling's is not
                # (e.g. the Instagram scope was declined) - carry on so the
                # Facebook Pages still connect.
                if key == platform:
                    raise
                log.warning("[oauth:%s] linked %s discovery skipped: %s",
                            platform, key, exc)
                accounts = []
            if accounts:
                results.extend((key, info) for info in accounts)
            else:
                empty.append(key)

        log.info("[oauth:%s] 3/4 discovery returned %d publishable account(s) "
                 "across %d platform(s)", platform, len(results),
                 len(OAuthManager._group_members(platform)))
        return bundle, results, empty
