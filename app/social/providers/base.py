"""The provider/adapter contract.

Every platform implements this interface and declares its Capabilities.
This is the seam that keeps platform logic out of business logic: services
depend on SocialProvider, never on a concrete adapter.
"""

from abc import ABC, abstractmethod

from app.social.dto import (
    AccountInfo,
    Capabilities,
    PostContent,
    PublishStep,
    TokenBundle,
)
from app.social.errors import SocialError


class SocialProvider(ABC):
    #: Stable platform key, e.g. "instagram", "facebook", "linkedin".
    key: str = ""
    #: Declared once per adapter (see dto.Capabilities).
    capabilities: Capabilities = None
    #: Whether this platform has its OWN connect (OAuth) entry point.
    #: False for platforms that are DISCOVERED through another's consent -
    #: e.g. Instagram Business accounts are found via the Facebook login and
    #: published with the linked Page token, so they are never connected on
    #: their own (Buffer / Meta Business Suite architecture).
    connectable: bool = True

    # -- OAuth -------------------------------------------------------------

    @abstractmethod
    def build_oauth_url(self, state: str, redirect_uri: str) -> str:
        """Consent-screen URL to redirect the user to."""

    @abstractmethod
    def exchange_code(
        self, code: str, code_verifier: str | None, redirect_uri: str
    ) -> TokenBundle:
        """Exchange an authorization code for tokens."""

    @abstractmethod
    def list_publishable_accounts(self, token: str) -> list[AccountInfo]:
        """Assets this token can publish as (Pages / IG business / org /
        channel)."""

    def refresh_token(self, account) -> TokenBundle | None:
        """Refresh an expiring token. Return None if the platform's tokens
        do not expire (e.g. Meta System-User)."""
        return None

    # -- Publishing (state machine) ---------------------------------------

    @abstractmethod
    def validate(self, content: PostContent) -> list[str]:
        """Pre-flight errors (empty list = ok). Checks content against
        this platform's Capabilities/specs before anything is uploaded."""

    @abstractmethod
    def start_publish(
        self, target, content: PostContent, token: str
    ) -> PublishStep:
        """Begin publishing. May return DONE, or PENDING with
        provider_state for a platform that processes asynchronously."""

    def poll_publish(
        self, target, provider_state: dict, token: str
    ) -> PublishStep:
        """Advance an async publish. Only called when start_publish
        returned PENDING; adapters that never go async need not override."""
        raise NotImplementedError

    # -- Analytics ---------------------------------------------------------

    def fetch_analytics(self, target, token: str) -> dict:
        """Latest insights for a published target. Default: nothing."""
        return {}

    # -- Errors ------------------------------------------------------------

    @abstractmethod
    def map_error(self, exc: Exception) -> SocialError:
        """Classify a raw exception/HTTP error into a typed SocialError so
        the retry engine can act without platform knowledge."""
