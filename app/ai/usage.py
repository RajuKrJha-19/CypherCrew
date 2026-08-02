"""AI usage logging, the monthly summary, and the budget cap.

Everything here is best-effort: logging never raises into an AI call, and the
budget check FAILS OPEN (allows the call) on any error - a spend log must never
be the reason a feature breaks.
"""
from datetime import datetime

from flask import current_app

from app.ai import pricing


def _month_start(now=None):
    now = now or datetime.utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def record(*, feature, provider, model, input_tokens=0, output_tokens=0,
           status="ok", actor_id=None, client_id=None):
    """Write one AIUsage row. Swallows every error (best-effort). Returns the
    new row's id (so a caller can later record its outcome), or None."""
    try:
        from app.extensions import db
        from app.models import AIUsage
        row = AIUsage(
            feature=feature, provider=provider, model=model,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            est_cost_usd=pricing.estimate(model, input_tokens, output_tokens),
            status=status, user_id=actor_id, client_id=client_id,
        )
        db.session.add(row)
        db.session.commit()
        return row.id
    except Exception:  # noqa: BLE001 - logging must never break the feature
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


_VALID_OUTCOMES = {"used", "discarded"}


def set_outcome(usage_id, outcome, actor_id=None):
    """Record whether the human kept an AI output. Best-effort and defensive:
    only a known value, only the row's own creator, and only once (never
    overwrites an existing outcome). Returns True iff it set anything."""
    if outcome not in _VALID_OUTCOMES:
        return False
    try:
        from app.extensions import db
        from app.models import AIUsage
        row = AIUsage.query.get(usage_id)
        if row is None or row.outcome is not None:
            return False
        # A row tied to a user may only be resolved by that user.
        if (actor_id is not None and row.user_id is not None
                and row.user_id != actor_id):
            return False
        row.outcome = outcome
        db.session.commit()
        return True
    except Exception:  # noqa: BLE001 - a metric must never break a workflow
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def month_total_usd():
    """Estimated AI spend so far this (UTC) month. 0.0 on error."""
    try:
        from app.extensions import db
        from app.models import AIUsage
        total = (db.session.query(db.func.coalesce(
            db.func.sum(AIUsage.est_cost_usd), 0.0))
            .filter(AIUsage.created_at >= _month_start()).scalar())
        return float(total or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def budget_usd():
    """The configured monthly cap, or 0.0 for no cap."""
    try:
        from app.models import AISettings
        row = AISettings.query.first()
        return float(getattr(row, "monthly_budget_usd", 0) or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def within_budget():
    """True if AI may run right now. FAILS OPEN: no cap, or any error -> True."""
    cap = budget_usd()
    if cap <= 0:
        return True
    try:
        return month_total_usd() < cap
    except Exception:  # noqa: BLE001
        return True


def month_summary(recent=25):
    """Numbers for the AI Usage screen: month total, cap, and breakdowns."""
    from app.extensions import db
    from app.models import AIUsage, Client

    start = _month_start()
    base = AIUsage.query.filter(AIUsage.created_at >= start)

    by_feature = dict(
        db.session.query(AIUsage.feature,
                         db.func.coalesce(db.func.sum(AIUsage.est_cost_usd), 0.0))
        .filter(AIUsage.created_at >= start)
        .group_by(AIUsage.feature).all())

    client_rows = (
        db.session.query(Client.client_name,
                         db.func.coalesce(db.func.sum(AIUsage.est_cost_usd), 0.0))
        .join(Client, Client.id == AIUsage.client_id)
        .filter(AIUsage.created_at >= start)
        .group_by(Client.client_name)
        .order_by(db.func.sum(AIUsage.est_cost_usd).desc())
        .limit(8).all())

    # Keep-rate per feature: of the drafts that got a signal, how many were
    # kept. cost + keep-rate together = the real ROI read.
    keep = {}
    for feat, outcome, n in (
            db.session.query(AIUsage.feature, AIUsage.outcome, db.func.count())
            .filter(AIUsage.created_at >= start, AIUsage.outcome.isnot(None))
            .group_by(AIUsage.feature, AIUsage.outcome).all()):
        bucket = keep.setdefault(feat, {"used": 0, "discarded": 0})
        if outcome in bucket:
            bucket[outcome] = int(n)

    return {
        "total": month_total_usd(),
        "budget": budget_usd(),
        "calls": base.count(),
        "by_feature": by_feature,
        "by_client": client_rows,
        "keep": keep,
        "recent": base.order_by(AIUsage.created_at.desc()).limit(recent).all(),
    }
