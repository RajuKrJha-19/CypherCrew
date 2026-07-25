"""Typed errors for the social engine.

Every provider adapter maps its platform's raw error codes into one of
these four types (via SocialProvider.map_error), so the retry engine can
decide what to do without knowing anything platform-specific:

- RateLimitError  -> reschedule past the window (not a hard attempt)
- TransientError  -> exponential backoff, capped by max_attempts
- AuthError       -> mark the account needs_reauth, stop retrying it
- PermanentError  -> dead-letter, surface for manual action
"""


class SocialError(Exception):
    """Base for all social engine errors."""

    def __init__(self, message, *, code=None, retry_after=None):
        super().__init__(message)
        self.message = message
        # Platform-native code/subcode, kept for logging/audit.
        self.code = code
        # Seconds to wait before retrying, when the platform tells us.
        self.retry_after = retry_after


class TransientError(SocialError):
    """Temporary failure (5xx, network, still-processing). Retry with
    backoff."""


class RateLimitError(SocialError):
    """Throttled / quota exhausted. Retry after the window; does not count
    against the hard attempt budget."""


class AuthError(SocialError):
    """Token invalid/expired/revoked. The account must be re-authorised;
    stop retrying until it is."""


class PermanentError(SocialError):
    """Validation / policy / unrecoverable error. Dead-letter it."""
