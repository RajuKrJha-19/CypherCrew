"""The meeting provider contract.

Abstract where backends genuinely differ, with working defaults where most
have nothing to do - the same split app/social/providers/base.py uses.
Jitsi needs no room provisioning call at all, so `create_room` and
`end_room` are no-ops here rather than abstract methods every adapter has
to stub out.
"""

from abc import ABC, abstractmethod


class MeetingProvider(ABC):
    #: Stable key, stored on Meeting.provider. Never changes once shipped -
    #: a meeting records which backend served it so that changing the
    #: default later cannot strand a call already in progress.
    key: str = ""

    #: What people read.
    display_name: str = ""

    #: Whether the call can be embedded in our own page, or only opened
    #: elsewhere. Google Meet and Zoom would be False.
    supports_embed: bool = True

    supports_recording: bool = False

    @abstractmethod
    def room_name(self, meeting) -> str:
        """The backend's identifier for this meeting's room."""

    @abstractmethod
    def join_url(self, meeting, user):
        """A URL that opens the call outside our page.

        The fallback when an embed is blocked - a corporate policy, a
        browser that refuses camera access in an iframe, or a user who
        simply prefers the full app.
        """

    def embed_config(self, meeting, user, moderator=False) -> dict:
        """Options for the in-page embed. {} when supports_embed is False."""
        return {}

    def create_room(self, meeting) -> None:
        """Provision the room, for backends that need it. Jitsi does not -
        a room exists the moment somebody joins it."""
        return None

    def end_room(self, meeting) -> None:
        """Tear the room down, for backends that need it."""
        return None
