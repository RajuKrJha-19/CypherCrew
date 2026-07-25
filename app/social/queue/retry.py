"""RetryEngine - decides what happens to a job after a failure, using only
the typed SocialError class (never platform specifics).

- RateLimitError -> reschedule past the window; NOT a hard attempt.
- TransientError -> exponential backoff + jitter, capped by max_attempts,
  then dead-letter.
- AuthError      -> fail the job; the account is flipped to needs_reauth
  by the worker and no more of its jobs run until re-auth.
- PermanentError -> dead-letter immediately.
"""

import random
from datetime import datetime, timedelta

from app.social.errors import (
    RateLimitError,
    TransientError,
    AuthError,
    PermanentError,
    SocialError,
)

_MAX_BACKOFF_SECONDS = 3600


def classify_and_schedule(job, error) -> str:
    """Mutate the job in place (state / attempts / next_run_at / last_error)
    and return a short outcome label. Does not commit."""
    if not isinstance(error, SocialError):
        error = PermanentError(str(error))

    job.last_error = (getattr(error, "message", None) or str(error))[:2000]

    if isinstance(error, RateLimitError):
        wait = error.retry_after or 900
        job.next_run_at = datetime.utcnow() + timedelta(seconds=wait)
        job.state = "queued"
        return "rate_limited"

    if isinstance(error, AuthError):
        job.state = "failed"
        return "auth_failed"

    if isinstance(error, TransientError):
        job.attempts += 1
        if job.attempts >= job.max_attempts:
            job.state = "dead"
            return "dead"
        backoff = min(_MAX_BACKOFF_SECONDS, (2 ** job.attempts) * 30)
        backoff += random.randint(0, 30)  # jitter
        job.next_run_at = datetime.utcnow() + timedelta(seconds=backoff)
        job.state = "queued"
        return "retry"

    # PermanentError (or anything unclassified)
    job.attempts += 1
    job.state = "dead"
    return "dead"
