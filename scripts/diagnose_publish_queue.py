"""Why is the publish queue stuck? Read-only.

Run on the server:

    cd /home/cyphercrew
    python scripts/diagnose_publish_queue.py

Changes nothing. Prints the state of every unfinished publish job, the rate
budget for each account, and the last error on anything stalled - which is
what separates the three things that look identical from the outside:

  * the worker is not running          -> everything sits in "queued", past due
  * the platform cap is spent          -> "queued" with next_run_at ~30 min out
  * a job is stranded in flight        -> "claimed"/"publishing", old locked_at
  * the platform keeps refusing it     -> attempts climbing, last_error set
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("AUTO_SEED", "false")
os.environ.setdefault("SOCIAL_INPROCESS_WORKER", "false")
os.environ.setdefault("ATTENDANCE_INPROCESS_WORKER", "false")

from app import create_app                                   # noqa: E402
from app.extensions import db                                # noqa: E402
from app.models import (                                     # noqa: E402
    PlatformRateBudget, PublishJob, SocialAccount, SocialPost,
    SocialPostTarget,
)
from app.social.registry import get_provider                 # noqa: E402


def age(when):
    if not when:
        return "-"
    delta = datetime.utcnow() - when
    minutes = int(delta.total_seconds() // 60)
    if abs(minutes) < 60:
        return "%dm" % minutes
    return "%.1fh" % (minutes / 60)


app = create_app()
with app.app_context():
    now = datetime.utcnow()
    print("server UTC now:", now.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78)

    # ---- 1. Is anything waiting that should already have run? -------------
    print("\n1. PUBLISH JOBS BY STATE")
    rows = (db.session.query(PublishJob.state, db.func.count(PublishJob.id))
            .group_by(PublishJob.state).all())
    for state, count in sorted(rows):
        print("   %-16s %d" % (state, count))
    if not rows:
        print("   (no jobs at all)")

    unfinished = (PublishJob.query
                  .filter(PublishJob.state.notin_(["succeeded"]))
                  .order_by(PublishJob.id.desc()).limit(40).all())

    print("\n2. UNFINISHED JOBS (newest 40)")
    print("   %-6s %-11s %-11s %-4s %-9s %-9s %s"
          % ("job", "platform", "state", "try", "next_run", "locked", "error"))
    for job in unfinished:
        target = db.session.get(SocialPostTarget, job.target_id)
        due = "-"
        if job.next_run_at:
            delta = (job.next_run_at - now).total_seconds()
            due = ("%+dm" % (delta // 60)) if abs(delta) >= 60 else "now"
        print("   %-6s %-11s %-11s %-4s %-9s %-9s %s"
              % (job.id, target.platform if target else "?", job.state,
                 job.attempts, due, age(job.locked_at),
                 (job.last_error or "")[:44]))
    if not unfinished:
        print("   (nothing unfinished - the queue is clear)")

    # ---- 3. Targets the post rollup is waiting on -------------------------
    print("\n3. TARGETS NOT SETTLED  (a post stays 'in queue' until every")
    print("   one of its targets is published, failed or blocked)")
    stuck = (SocialPostTarget.query
             .filter(SocialPostTarget.status.notin_(
                 ["published", "failed", "blocked", "removed", "draft"]))
             .order_by(SocialPostTarget.id.desc()).limit(40).all())
    print("   %-7s %-11s %-11s %-9s %s"
          % ("target", "platform", "status", "scheduled", "post"))
    for t in stuck:
        post = db.session.get(SocialPost, t.social_post_id)
        print("   %-7s %-11s %-11s %-9s %s"
              % (t.id, t.platform, t.status, age(t.scheduled_for),
                 ("#%s %s" % (post.id, (post.title or "")[:26]))
                 if post else "?"))
    if not stuck:
        print("   (none)")

    # ---- 4. The platform caps --------------------------------------------
    print("\n4. RATE BUDGETS  (YouTube is only 6 uploads / 24h - once that is")
    print("   spent, every further job defers 30 minutes at a time)")
    print("   %-24s %-10s %-7s %-8s %s"
          % ("account", "platform", "used", "limit", "window started"))
    for budget in PlatformRateBudget.query.all():
        account = db.session.get(SocialAccount, budget.social_account_id)
        provider = get_provider(account.platform) if account else None
        caps = getattr(provider, "capabilities", None)
        limit = (caps.publish_rate[0]
                 if caps and caps.publish_rate else "-")
        spent = (isinstance(limit, int) and budget.used_count >= limit)
        print("   %-24s %-10s %-7s %-8s %s  %s"
              % ((account.display_name if account else "?")[:24],
                 account.platform if account else "?",
                 budget.used_count, limit, age(budget.window_start) + " ago",
                 "<-- SPENT" if spent else ""))
    if not PlatformRateBudget.query.count():
        print("   (no budget rows yet)")

    # ---- 5. Could the account even publish? -------------------------------
    print("\n5. ACCOUNTS")
    for account in SocialAccount.query.order_by(SocialAccount.platform).all():
        provider = get_provider(account.platform)
        print("   %-24s %-10s status=%-14s adapter=%s"
              % (account.display_name[:24], account.platform, account.status,
                 type(provider).__name__ if provider else "NONE"))

    # ---- 6. Is the worker actually turning? -------------------------------
    print("\n6. IS THE WORKER RUNNING?")
    recent = (PublishJob.query
              .filter(PublishJob.state == "succeeded")
              .order_by(PublishJob.id.desc()).first())
    print("   SOCIAL_INPROCESS_WORKER :",
          app.config.get("SOCIAL_INPROCESS_WORKER"))
    print("   SOCIAL_WORKER_INTERVAL  :",
          app.config.get("SOCIAL_WORKER_INTERVAL"), "seconds")
    print("   SOCIAL_WORKER_TOKEN set :",
          bool(app.config.get("SOCIAL_WORKER_TOKEN")),
          "(needed only for the /internal cron endpoints)")
    print("   last successful publish :",
          age(recent.updated_at) + " ago" if recent and recent.updated_at
          else "never")

    overdue = (PublishJob.query
               .filter(PublishJob.state == "queued",
                       PublishJob.next_run_at < now - timedelta(minutes=3))
               .count())
    print("   jobs due >3m ago, still queued:", overdue,
          "  <-- if this is not 0, the worker is NOT draining" if overdue
          else "")

    print("\n" + "=" * 78)
    print("Paste this whole output back and it will say which of the four it is.")
