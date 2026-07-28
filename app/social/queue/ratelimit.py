"""Per-(account, window) rolling-window publish gate.

Enforces platform caps (e.g. Instagram's 100 posts / 24h) so the worker
defers over-budget jobs instead of getting throttled by the platform.
Reserve-then-publish: a job reserves a slot before it publishes; on a hard
failure the slot is released so it isn't wasted.
"""

from datetime import datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.extensions import db
from app.models import PlatformRateBudget


_WINDOWS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
}


def _window_delta(window):
    return _WINDOWS.get(window, timedelta(hours=24))


def _budget_row(account_id, window):
    # Two targets for the SAME account can publish concurrently (the worker's
    # thread pool - e.g. a feed post and its companion Story). A plain
    # SELECT-then-INSERT let both threads see no row and both INSERT, so the
    # second hit the unique (account, window) constraint and stranded that
    # target ("partially published"). Upsert-then-lock is race-safe: the
    # ON CONFLICT DO NOTHING guarantees the row exists (the loser blocks on the
    # unique index until the winner commits, then no-ops), and the subsequent
    # SELECT ... FOR UPDATE serialises the used_count update.
    db.session.execute(
        pg_insert(PlatformRateBudget.__table__)
        .values(social_account_id=account_id, rate_window=window,
                window_start=datetime.utcnow(), used_count=0)
        .on_conflict_do_nothing(
            index_elements=["social_account_id", "rate_window"])
    )
    row = (
        PlatformRateBudget.query
        .filter_by(social_account_id=account_id, rate_window=window)
        .with_for_update()
        .first()
    )
    # Roll the window over if it has elapsed.
    if datetime.utcnow() - row.window_start >= _window_delta(window):
        row.window_start = datetime.utcnow()
        row.used_count = 0
    return row


def reserve(account_id, limit, window="24h") -> bool:
    """Reserve one slot. True if within budget (and reserved), else False.
    Caller must be inside a transaction (the worker is)."""
    row = _budget_row(account_id, window)
    if row.used_count >= limit:
        return False
    row.used_count += 1
    return True


def release(account_id, window="24h"):
    """Give a reserved slot back after a hard failure."""
    row = _budget_row(account_id, window)
    if row.used_count > 0:
        row.used_count -= 1
