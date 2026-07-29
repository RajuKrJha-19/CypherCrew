"""Provider registry: the single lookup from a provider key to its adapter.

A near-copy of app/social/registry.py, for the same reason it exists there:
services call `get_provider(meeting.provider)` and never import a backend,
so adding one touches its own file plus the import list below.
"""

from app.teams.providers.base import MeetingProvider


class MeetingRegistry:
    def __init__(self):
        self._providers: dict[str, MeetingProvider] = {}

    def register(self, provider: MeetingProvider) -> None:
        if not provider.key:
            raise ValueError("Provider must define a non-empty key")
        self._providers[provider.key] = provider

    def get(self, key: str):
        return self._providers.get(key)

    def all(self) -> dict[str, MeetingProvider]:
        return dict(self._providers)

    def keys(self) -> list[str]:
        return list(self._providers)


#: Process-wide singleton.
registry = MeetingRegistry()


def get_provider(key=None):
    """The adapter for `key`, falling back to the configured default.

    Falls back rather than returning None because a meeting row always
    carries a provider string, and a row written by an older deploy (or by
    hand) must still be joinable. A meeting whose provider no longer exists
    is better served by the current one than by a 500.
    """
    from flask import current_app

    provider = registry.get(key) if key else None
    if provider is not None:
        return provider

    default = current_app.config.get("TEAMS_MEETING_PROVIDER", "jitsi")
    return registry.get(default)


def load_meeting_providers(app=None):
    """Register the active adapters at startup (only when TEAMS_ENABLED)."""
    from app.teams.providers.jitsi import JitsiProvider

    registry.register(JitsiProvider())
