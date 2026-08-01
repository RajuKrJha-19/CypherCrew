"""Typed AI errors, so routes can map failures to clean HTTP responses without
knowing which backend produced them - mirrors app/social/errors.py.

Rule: a message here is a provider status/summary, NEVER an API key or the
request body. Nothing in this module (or its callers) logs the key.
"""


class AIError(Exception):
    """Base for every AI-layer failure."""


class AIDisabled(AIError):
    """AI is off (flag unset) or no backend is configured. -> 503."""


class AIAuth(AIError):
    """The provider rejected the credential (misconfig). -> 502, alert ops."""


class AITransient(AIError):
    """Rate limit / overload / timeout / 5xx - worth one retry. -> 503."""


class AIPermanent(AIError):
    """Bad request or unusable output that a retry won't fix. -> 502."""
