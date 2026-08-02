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
    """Write one AIUsage row. Swallows every error (best-effort)."""
    try:
        from app.extensions import db
        from app.models import AIUsage
        db.session.add(AIUsage(
            feature=feature, provider=provider, model=model,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            est_cost_usd=pricing.estimate(model, input_tokens, output_tokens),
            status=status, user_id=actor_id, client_id=client_id,
        ))
        db.session.commit()
    except Exception:  # noqa: BLE001 - logging must never break the feature
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


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

    return {
        "total": month_total_usd(),
        "budget": budget_usd(),
        "calls": base.count(),
        "by_feature": by_feature,
        "by_client": client_rows,
        "recent": base.order_by(AIUsage.created_at.desc()).limit(recent).all(),
    }
