"""Provider registry: the single lookup from a platform key to its adapter.

Adapters register themselves on import. Business logic calls
`get_provider("instagram")` and never imports a platform module, so adding
a platform touches only its adapter file + the import list in
`load_providers()`.
"""

from app.social.providers.base import SocialProvider


class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, SocialProvider] = {}

    def register(self, provider: SocialProvider) -> None:
        if not provider.key:
            raise ValueError("Provider must define a non-empty key")
        self._providers[provider.key] = provider

    def get(self, key: str) -> SocialProvider | None:
        return self._providers.get(key)

    def all(self) -> dict[str, SocialProvider]:
        return dict(self._providers)

    def keys(self) -> list[str]:
        return list(self._providers)


#: Process-wide singleton.
registry = ProviderRegistry()


def get_provider(key: str) -> SocialProvider | None:
    return registry.get(key)


def load_providers(app=None) -> None:
    """Register the active providers at startup (only when
    SOCIAL_ENGINE_ENABLED).

    In SOCIAL_SIMULATION_MODE (the default until real adapters ship), the
    SimulationProvider is registered for every platform so the whole engine
    is exercisable end-to-end with no external credentials. Real adapters,
    added per phase, register under the same keys and take over.
    """
    from flask import current_app
    config = (app or current_app).config

    # Real adapters first, so they claim their platform keys.
    covered = set()

    # Meta (Facebook + Instagram): live when an app id is configured, or when
    # the local Graph emulator is on (which supplies dummy credentials). The
    # provider code is identical in both cases.
    if config.get("META_APP_ID") or config.get("META_EMULATOR"):
        from app.social.providers.meta_facebook import MetaFacebookProvider
        from app.social.providers.meta_instagram import MetaInstagramProvider
        registry.register(MetaFacebookProvider())
        registry.register(MetaInstagramProvider())
        covered.update({"facebook", "instagram"})

    # Google (YouTube + Business Profile): one OAuth client serves both,
    # but they are separate connects - a business may have a Profile and no
    # channel, or the reverse. Each is registered only when its own API is
    # switched on, so a project with YouTube enabled and Business Profile
    # still pending approval gets the real YouTube adapter and a demo
    # Business channel rather than a broken one.
    if config.get("GOOGLE_CLIENT_ID") and config.get("GOOGLE_CLIENT_SECRET"):
        if config.get("YOUTUBE_ENABLED", True):
            from app.social.providers.youtube import YouTubeProvider
            registry.register(YouTubeProvider())
            covered.add("youtube")
        if config.get("GOOGLE_BUSINESS_ENABLED", True):
            from app.social.providers.google_business import (
                GoogleBusinessProvider,
            )
            registry.register(GoogleBusinessProvider())
            covered.add("google_business")

    # Simulation fills every platform that has no real adapter yet, so the
    # engine stays exercisable end-to-end.
    if config.get("SOCIAL_SIMULATION_MODE", True):
        from app.social.providers.simulation import (
            register_simulation_providers, SIM_PLATFORMS,
        )
        register_simulation_providers(
            registry, [p for p in SIM_PLATFORMS if p not in covered])
    return
